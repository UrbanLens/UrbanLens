"""The site-admin system panel reports media storage without walking the media tree on the request."""

from __future__ import annotations

from datetime import timedelta
import os
import pathlib
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.admin import media_usage
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.tasks import measure_media_usage_task

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


class SystemPanelMediaUsageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        user = baker.make(User)
        add_user_to_site_admin_group(user)
        self.client.force_login(user)
        for key in (media_usage.CACHE_KEY, media_usage.GUARD_KEY):
            cache.delete(key)
            self.addCleanup(cache.delete, key)

    def _panel(self) -> object:
        return self.client.get(reverse("site_admin_stats_system"))

    def _store(self, *, megabytes: float, age: timedelta) -> None:
        measured_at = (timezone.now() - age).isoformat()
        cache.set(media_usage.CACHE_KEY, {"megabytes": megabytes, "measured_at": measured_at}, 3600)

    @staticmethod
    def _measurements_requested(enqueue: mock.Mock) -> int:
        return sum(1 for call in enqueue.call_args_list if call.args and call.args[0] is measure_media_usage_task)

    def test_the_panel_never_walks_the_media_tree(self) -> None:
        """It polls every 60 seconds, and the tree is every file the site stores."""
        with mock.patch("os.walk", wraps=os.walk) as walk, tasks_run_inline():
            response = self._panel()

        self.assertEqual(response.status_code, 200)
        walked = {str(call.args[0]) for call in walk.call_args_list if call.args}
        self.assertNotIn(str(settings.MEDIA_ROOT), walked)

    def test_a_missing_measurement_is_asked_for_once_however_often_the_panel_polls(self) -> None:
        with mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            first = self._panel()
            self._panel()

        self.assertContains(first, "Measuring")
        self.assertEqual(self._measurements_requested(enqueue), 1)

    def test_a_fresh_measurement_is_shown_and_not_retaken(self) -> None:
        self._store(megabytes=12.5, age=timedelta(minutes=5))

        with mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            response = self._panel()

        self.assertContains(response, "12.5 MB")
        self.assertEqual(self._measurements_requested(enqueue), 0)

    def test_a_stale_measurement_is_shown_while_the_next_is_taken(self) -> None:
        self._store(megabytes=12.5, age=media_usage.FRESH_FOR + timedelta(minutes=1))

        with mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            response = self._panel()

        self.assertContains(response, "12.5 MB")
        self.assertEqual(self._measurements_requested(enqueue), 1)

    def test_the_task_measures_and_the_next_poll_shows_it(self) -> None:
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            pathlib.Path(root, "photo.bin").write_bytes(b"x" * 1_048_576)
            with tasks_run_inline(measure_media_usage_task):
                self._panel()
            response = self._panel()

        self.assertContains(response, "1.0 MB")
        self.assertIsNone(single_flight.holder(media_usage.GUARD_KEY))
