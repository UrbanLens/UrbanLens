"""A deferred cid batch keeps trying for two days, at widening intervals.

A large import routinely leaves hundreds of cids needing a live REData/Places
lookup. The task used to give up after five consecutive no-progress rounds -
about ten minutes - and convert every unresolved cid into a `PinImportFailure`,
so a single import produced 600+ rows for the user to fix by hand even though
most of those cids resolve on their own within the hour.

The counters now only choose how far apart the retries are; the batch ends when
it is older than ``_DEFERRED_LOOKUP_DEADLINE``. The spacing widens sharply after
the first few attempts, because REData will not re-queue a cid it has already
checked for weeks - asking every two minutes for two days is load with no new
answer - while the first few stay short so a batch waiting on a rate limit
clears quickly.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult

#: Position of started_at in retry()'s args list, counted from the start.
_ARG_STARTED_AT = 6


class DeferredLookupRetryWindowTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.deferred_lists = [
            {
                "stem": "",
                "create_category": False,
                "label_ids": [],
                "pins": [
                    {"name": "Black Point Ruins", "lat": 41.348754, "lng": -71.453896, "description": "", "cid": 111}
                ],
            },
        ]

    def _run(self, **kwargs):
        pending = CidResolutionResult(provider=PROVIDER_REDATA, pending=[111], request_failed=False)
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=pending),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as retry,
        ):
            tasks.resolve_deferred_pin_locations(self.profile.pk, self.deferred_lists, auto_tag=False, **kwargs)
        return retry

    def test_the_old_cap_no_longer_ends_the_batch(self) -> None:
        """This is the regression that produced 600+ failure rows per import."""
        retry = self._run(consecutive_no_progress=tasks._MAX_CONSECUTIVE_NO_PROGRESS_RETRIES + 3)

        retry.assert_called_once()
        self.assertFalse(PinImportFailure.objects.filter(profile=self.profile).exists())

    def test_the_first_attempts_retry_quickly(self) -> None:
        """A batch waiting on a rate limit should not sit idle for hours."""
        retry = self._run(consecutive_no_progress=0)

        self.assertLessEqual(retry.call_args.kwargs["countdown"], 300)

    def test_later_attempts_back_off_a_long_way(self) -> None:
        retry = self._run(consecutive_no_progress=9)

        self.assertGreaterEqual(retry.call_args.kwargs["countdown"], 3600)

    def test_the_batch_start_is_carried_into_the_retry(self) -> None:
        """Without it every retry would restart the two-day window."""
        started = (timezone.now() - timedelta(hours=5)).isoformat()

        retry = self._run(consecutive_no_progress=1, started_at=started)

        self.assertEqual(retry.call_args.kwargs["args"][_ARG_STARTED_AT], started)

    def test_a_first_attempt_stamps_a_start(self) -> None:
        retry = self._run(consecutive_no_progress=0)

        self.assertTrue(retry.call_args.kwargs["args"][_ARG_STARTED_AT], "the batch never recorded when it began")

    def test_a_batch_past_the_deadline_stops_and_records_failures(self) -> None:
        retry = self._run(
            consecutive_no_progress=1,
            started_at=(timezone.now() - tasks._DEFERRED_LOOKUP_DEADLINE - timedelta(minutes=1)).isoformat(),
        )

        retry.assert_not_called()
        self.assertTrue(PinImportFailure.objects.filter(profile=self.profile, cid=111).exists())

    def test_the_window_covers_two_days_in_a_modest_number_of_attempts(self) -> None:
        """The point of widening: two days of cover without hammering REData."""
        total = 0.0
        attempts = 0
        while total < tasks._DEFERRED_LOOKUP_DEADLINE.total_seconds():
            total += tasks._deferred_retry_countdown(attempts)
            attempts += 1

        self.assertLess(attempts, 40, f"two days costs {attempts} attempts - the backoff is too shallow")
        self.assertGreater(attempts, 8, "too few attempts to give a slow batch several chances")

    def test_a_naive_start_stamp_does_not_kill_the_task(self) -> None:
        """The only producer stamps an aware timestamp, but a replayed or
        hand-enqueued message can carry a naive one. Subtracting it raises
        TypeError - not the ValueError the parse guard catches - which would kill
        the task rather than retire the batch."""
        from urbanlens.dashboard.tasks import _deferred_deadline_passed

        self.assertFalse(_deferred_deadline_passed(timezone.now().replace(tzinfo=None).isoformat()))

    def test_a_naive_start_stamp_still_expires(self) -> None:
        """Falling back to "not expired" on a naive stamp would let the batch
        retry forever, defeating the deadline entirely."""
        from urbanlens.dashboard.tasks import _deferred_deadline_passed

        long_ago = (timezone.now() - timedelta(days=3)).replace(tzinfo=None)

        self.assertTrue(_deferred_deadline_passed(long_ago.isoformat()))

    def test_an_unparseable_start_stamp_is_ignored(self) -> None:
        from urbanlens.dashboard.tasks import _deferred_deadline_passed

        self.assertFalse(_deferred_deadline_passed("not-a-timestamp"))
