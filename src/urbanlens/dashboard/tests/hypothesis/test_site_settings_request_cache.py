"""Tests for the request-scoped memoisation of the SiteSettings singleton.

The memo has to satisfy two opposing requirements, so both are pinned here: it
must collapse the several identical singleton fetches a single page render does,
*and* it must not survive the request - long-lived Celery workers and the test
suite's own ``queryset.update()`` edits would both read a stale row otherwise.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.contrib.auth.models import User
from django.core.signals import request_finished, request_started
from django.db import close_old_connections, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import request_cache
from urbanlens.dashboard.models.site_settings.model import SiteSettings

_TABLE = "dashboard_site_settings"


def _settings_query_count(captured: CaptureQueriesContext) -> int:
    return len([query for query in captured.captured_queries if _TABLE in query["sql"]])


@contextmanager
def simulated_request():
    """Fire the request lifecycle signals exactly as Django's handlers do.

    ``close_old_connections`` is disconnected around the signals the same way
    ``django.test.client.ClientHandler`` disconnects it - left connected, it would
    tear down the connection holding this test's transaction.
    """
    request_started.disconnect(close_old_connections)
    request_finished.disconnect(close_old_connections)
    try:
        request_started.send(sender=simulated_request)
        yield
        request_finished.send(sender=simulated_request)
    finally:
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)


class SiteSettingsOutsideARequestTests(TestCase):
    """No request, no memo - management commands and Celery tasks read through."""

    def setUp(self) -> None:
        super().setUp()
        SiteSettings.get_current()  # ensure the singleton row exists

    def test_repeated_calls_each_hit_the_database(self) -> None:
        with CaptureQueriesContext(connection) as captured:
            SiteSettings.get_current()
            SiteSettings.get_current()
        self.assertEqual(_settings_query_count(captured), 2)

    def test_a_row_updated_behind_save_is_still_seen(self) -> None:
        """``queryset.update()`` bypasses ``save()``, so nothing invalidates a cache -
        which is exactly why this context must not have one."""
        SiteSettings.objects.filter(pk=1).update(max_pins_per_list=7)
        self.assertEqual(SiteSettings.get_current().max_pins_per_list, 7)


class SiteSettingsDuringARequestTests(TestCase):
    """Within one request the singleton is fetched once, however many callers ask."""

    def setUp(self) -> None:
        super().setUp()
        SiteSettings.get_current()

    def test_repeated_calls_share_one_fetch(self) -> None:
        with simulated_request(), CaptureQueriesContext(connection) as captured:
            first = SiteSettings.get_current()
            second = SiteSettings.get_current()
            third = SiteSettings.get_current()
        self.assertEqual(_settings_query_count(captured), 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.pk, third.pk)

    def test_the_scope_is_torn_down_when_the_request_ends(self) -> None:
        with simulated_request():
            SiteSettings.get_current()
            self.assertIsNotNone(request_cache.get_cached())
        self.assertIsNone(request_cache.get_cached())

    def test_the_memo_does_not_leak_into_the_next_request(self) -> None:
        with simulated_request():
            SiteSettings.get_current()

        SiteSettings.objects.filter(pk=1).update(max_pins_per_list=3)

        with simulated_request(), CaptureQueriesContext(connection) as captured:
            self.assertEqual(SiteSettings.get_current().max_pins_per_list, 3)
        self.assertEqual(_settings_query_count(captured), 1)

    def test_saving_settings_mid_request_is_visible_immediately(self) -> None:
        """An admin editing settings must see their own change on the rest of that request."""
        with simulated_request():
            current = SiteSettings.get_current()
            current.max_pins_per_list = 11
            current.save(update_fields=["max_pins_per_list", "updated"])
            self.assertEqual(SiteSettings.get_current().max_pins_per_list, 11)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SiteSettingsRealRequestTests(TestCase):
    """End to end: a genuine page render, whose many callers collapse to one fetch.

    The cache is pinned to locmem because the default backend is Redis-backed and the
    test suite's network guard only permits localhost connections.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        SiteSettings.get_current()

    def test_a_page_render_fetches_the_singleton_once(self) -> None:
        # Three context processors plus the view itself each ask for it independently.
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_settings_query_count(captured), 1)
