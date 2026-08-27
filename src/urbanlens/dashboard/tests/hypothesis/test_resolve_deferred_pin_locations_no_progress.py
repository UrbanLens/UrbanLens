"""Regression test: cids stuck pending on REData's own end must not retry forever.

REData's cid-resolution cache policy (StaggeredCachePolicy, min_ttl_hours=720 by
default - see ../REData's core.services.staggered_cache) has a hard minimum-TTL
floor: once a cid has been checked at all, REData won't queue another
resolution attempt for it for weeks, but keeps reporting it as "pending" (HTTP
200, result.request_failed=False) every time it's asked, since it's neither
resolved nor confirmed unresolvable. Before this fix,
resolve_deferred_pin_locations treated that identically to a batch that was
still making real progress and retried every ~120s forever with no cap - the
existing consecutive_request_failures cap only covers whole-batch request
failures, not "REData responded fine but nothing moved."

retry()'s args list is [profile_id, remaining_lists, auto_tag, total,
consecutive_request_failures, consecutive_no_progress] - tests index from the
end.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult

#: Positions in retry()'s args list, counted from the start so appending a
#: parameter cannot silently repoint them onto the wrong value.
_ARG_REQUEST_FAILURES = 4
_ARG_NO_PROGRESS = 5


class ResolveDeferredPinLocationsNoProgressTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.two_pin_lists = [
            {
                "stem": "",
                "create_category": False,
                "label_ids": [],
                "pins": [
                    {"name": "Black Point Ruins", "lat": 41.348754, "lng": -71.453896, "description": "", "cid": 111},
                    {"name": "Fort Wetherill", "lat": 41.4759, "lng": -71.3512, "description": "", "cid": 222},
                ],
            },
        ]

    def _all_pending_result(self) -> CidResolutionResult:
        return CidResolutionResult(provider=PROVIDER_REDATA, pending=[111, 222], request_failed=False)

    def test_no_progress_below_the_cap_still_retries(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=self._all_pending_result()),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as mock_retry,
        ):
            result = tasks.resolve_deferred_pin_locations(self.profile.pk, self.two_pin_lists, auto_tag=False)

        # throw=False: still-pending is the routine case, so the task returns
        # normally (scheduling its own retry silently) instead of raising - a raised
        # Retry would log a spurious WARNING + traceback for expected, ongoing work.
        self.assertEqual(result, {"created": 0, "exists": 0, "skipped": 0})
        mock_retry.assert_called_once()
        self.assertFalse(mock_retry.call_args.kwargs["throw"])
        self.assertEqual(mock_retry.call_args.kwargs["args"][_ARG_REQUEST_FAILURES], 0)
        self.assertEqual(mock_retry.call_args.kwargs["args"][_ARG_NO_PROGRESS], 1)
        self.assertFalse(NotificationLog.objects.filter(profile=self.profile).exists())

    def test_a_batch_past_the_deadline_gives_up_and_notifies_instead_of_retrying(self) -> None:
        """The cap now only widens the retry gap; two days of no progress ends it."""
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=self._all_pending_result()),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as mock_retry,
        ):
            result = tasks.resolve_deferred_pin_locations(
                self.profile.pk,
                self.two_pin_lists,
                auto_tag=False,
                consecutive_no_progress=tasks._MAX_CONSECUTIVE_NO_PROGRESS_RETRIES - 1,
                started_at=(timezone.now() - timedelta(days=3)).isoformat(),
            )

        mock_retry.assert_not_called()
        self.assertEqual(result, {"created": 0, "exists": 0, "skipped": 2})

        notification = NotificationLog.objects.get(profile=self.profile)
        self.assertEqual(notification.notification_type, NotificationType.ERROR)
        self.assertEqual(notification.importance, Importance.HIGH)

    def test_partial_progress_resets_the_no_progress_counter(self) -> None:
        """One cid resolving out of the batch is real progress, even if the rest are
        still pending - it must not inherit whatever no-progress streak preceded it,
        or a REData queue that's slowly working through a large batch would still get
        cut off early by a stale counter."""
        partial = CidResolutionResult(provider=PROVIDER_REDATA, resolved={111: (41.348754, -71.453896)}, pending=[222], request_failed=False)
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=partial),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as mock_retry,
        ):
            tasks.resolve_deferred_pin_locations(
                self.profile.pk,
                self.two_pin_lists,
                auto_tag=False,
                consecutive_no_progress=tasks._MAX_CONSECUTIVE_NO_PROGRESS_RETRIES - 1,
            )

        mock_retry.assert_called_once()
        self.assertEqual(mock_retry.call_args.kwargs["args"][_ARG_NO_PROGRESS], 0)
        # Only the still-pending pin should be carried into the retry.
        remaining_lists = mock_retry.call_args.kwargs["args"][1]
        remaining_cids = [p["cid"] for lst in remaining_lists for p in lst["pins"]]
        self.assertEqual(remaining_cids, [222])
        self.assertFalse(NotificationLog.objects.filter(profile=self.profile).exists())
