"""Regression test: a persistently unreachable REData must not retry forever.

Before this fix, resolve_deferred_pin_locations treated every non-auth REData
request failure (network error, non-200, unparseable body) as an ordinary
"pending" result and retried with max_retries=None - identical to a batch that
was making real progress on REData's own end. A REData outage (or a
misconfigured UL_REDATA_API_URL/UL_REDATA_API_KEY) would then retry every
120s forever, logging a WARNING + full traceback via the task_retry signal
(see UrbanLens/celery.py) on every single attempt with no cap and no user
notification - unlike the already-handled auth_failed case.
"""

from __future__ import annotations

from unittest import mock

from celery.exceptions import Retry
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult


class ResolveDeferredPinLocationsConsecutiveFailuresTests(TestCase):
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

    def _request_failed_result(self) -> CidResolutionResult:
        return CidResolutionResult(provider=PROVIDER_REDATA, pending=[12345], request_failed=True)

    def test_a_single_request_failure_still_retries(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=self._request_failed_result()),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry", side_effect=Retry("retrying")) as mock_retry,
        ):
            with self.assertRaises(Retry):
                tasks.resolve_deferred_pin_locations(self.profile.pk, self.deferred_lists, auto_tag=False)

        mock_retry.assert_called_once()
        self.assertEqual(mock_retry.call_args.kwargs["args"][-1], 1)
        self.assertFalse(NotificationLog.objects.filter(profile=self.profile).exists())

    def test_exceeding_the_cap_gives_up_and_notifies_instead_of_retrying(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=self._request_failed_result()),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry", side_effect=Retry("retrying")) as mock_retry,
        ):
            result = tasks.resolve_deferred_pin_locations(
                self.profile.pk,
                self.deferred_lists,
                auto_tag=False,
                consecutive_request_failures=tasks._MAX_CONSECUTIVE_REDATA_FAILURES - 1,
            )

        # A retry would have raised instead of returning - reaching this line at all
        # (with the mock never invoked) proves the task gave up rather than retrying again.
        mock_retry.assert_not_called()
        self.assertEqual(result, {"created": 0, "exists": 0, "skipped": 1})

        notification = NotificationLog.objects.get(profile=self.profile)
        self.assertEqual(notification.notification_type, NotificationType.ERROR)
        self.assertEqual(notification.importance, Importance.HIGH)

    def test_a_successful_response_resets_the_counter(self) -> None:
        """A batch that's still pending on REData's own end (not a request failure) is
        real progress - it must not inherit whatever failure streak preceded it, or a
        REData instance that recovers after N-1 failures would still get cut off early
        by a stale counter."""
        recovered = CidResolutionResult(provider=PROVIDER_REDATA, pending=[12345], request_failed=False)
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=recovered),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry", side_effect=Retry("retrying")) as mock_retry,
        ):
            with self.assertRaises(Retry):
                tasks.resolve_deferred_pin_locations(
                    self.profile.pk,
                    self.deferred_lists,
                    auto_tag=False,
                    consecutive_request_failures=tasks._MAX_CONSECUTIVE_REDATA_FAILURES - 1,
                )

        mock_retry.assert_called_once()
        # args = [profile_id, remaining_lists, auto_tag, total, consecutive_request_failures]
        self.assertEqual(mock_retry.call_args.kwargs["args"][-1], 0)
        self.assertFalse(NotificationLog.objects.filter(profile=self.profile).exists())
