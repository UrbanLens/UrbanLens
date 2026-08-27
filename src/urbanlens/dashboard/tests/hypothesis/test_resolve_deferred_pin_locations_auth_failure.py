"""Regression test: REData rejecting the API key must not retry forever.

Before this fix, resolve_deferred_pin_locations treated a 401/403 from REData
the same as a transient outage - the whole batch came back "pending" and the
task retried with max_retries=None, looping indefinitely against a permission
error that retrying can never clear (see cid_resolution.CidResolutionResult's
auth_failed field and this task's own auth_failed branch).
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult


class ResolveDeferredPinLocationsAuthFailureTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.deferred_lists = [
            {
                "stem": "",
                "create_category": False,
                "label_ids": [],
                "pins": [{"name": "Black Point Ruins", "lat": 41.348754, "lng": -71.453896, "description": "", "cid": 12345}],
            },
        ]

    def test_auth_failure_does_not_retry_and_notifies_the_user(self) -> None:
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids",
                return_value=CidResolutionResult(provider=PROVIDER_REDATA, pending=[12345], auth_failed=True),
            ),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            result = tasks.resolve_deferred_pin_locations(self.profile.pk, self.deferred_lists, auto_tag=False)

        # A retry would have raised celery.exceptions.Retry instead of returning - reaching
        # this line at all proves the task didn't loop back into self.retry().
        self.assertEqual(result, {"created": 0, "exists": 0, "skipped": 1})

        notification = NotificationLog.objects.get(profile=self.profile)
        self.assertEqual(notification.notification_type, NotificationType.ERROR)
        self.assertEqual(notification.importance, Importance.HIGH)
