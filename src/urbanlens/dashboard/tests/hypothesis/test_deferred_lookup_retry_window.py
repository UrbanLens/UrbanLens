"""A deferred cid batch keeps trying for two days, at widening intervals."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.cid_resolution import (
    PROVIDER_GOOGLE,
    PROVIDER_REDATA,
    CidResolutionResult,
)

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
        """The only producer stamps an aware timestamp, but a replayed or hand-enqueued message can carry a naive one. Subtracting it raises TypeError - not the ValueError the parse guard catches - which would kill the task rather than retire the batch."""
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


class DeferredLookupRetryBoundTests(TestCase):
    """The deadline ends a batch; ``max_retries`` is finite as well, so a batch the deadline cannot end still stops.

    ``_deferred_deadline_passed`` reads an unparseable ``started_at`` as "not expired", and the task re-sends the
    stamp it was given, so a replayed or hand-made message carrying one would retry for ever on the deadline alone.
    """

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

    def _apply(
        self,
        *,
        retries: int,
        request_failed: bool = False,
        provider: str = PROVIDER_REDATA,
        lookup: CidResolutionResult | None = None,
    ):
        """Run the task as a worker would on its ``retries``-th retry, with a start stamp the deadline cannot read.

        Celery's own ``retry()`` runs, so a retry past the bound raises ``MaxRetriesExceededError`` here as it would
        on a worker; only the re-sent message is stubbed.

        Returns:
            The stubbed signature of the retry message, and the task's return value.
        """
        pending = lookup or CidResolutionResult(provider=provider, pending=[111], request_failed=request_failed)
        args = [self.profile.pk, self.deferred_lists, False, 1, 0, 0, "not-a-timestamp"]
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=pending),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "signature_from_request") as resent,
        ):
            result = tasks.resolve_deferred_pin_locations.apply(args=args, retries=retries, throw=True)
        return resent, result.get()

    def test_the_task_has_a_finite_retry_bound(self) -> None:
        self.assertIsNotNone(
            tasks.resolve_deferred_pin_locations.max_retries, "a permanent failure can requeue the batch for ever"
        )

    def test_the_bound_never_cuts_a_batch_short_of_the_deadline(self) -> None:
        """Every retry waits at least the shortest gap, so the bound must not run out before the deadline does."""
        shortest_gap = min(*tasks._DEFERRED_RETRY_SCHEDULE, tasks._GOOGLE_RATE_LIMIT_RETRY_SECONDS)

        self.assertGreaterEqual(
            tasks.resolve_deferred_pin_locations.max_retries * shortest_gap,
            tasks._DEFERRED_LOOKUP_DEADLINE.total_seconds(),
        )

    def test_the_bound_is_no_looser_than_the_deadline_needs(self) -> None:
        shortest_gap = min(*tasks._DEFERRED_RETRY_SCHEDULE, tasks._GOOGLE_RATE_LIMIT_RETRY_SECONDS)

        self.assertLess(
            (tasks.resolve_deferred_pin_locations.max_retries - 1) * shortest_gap,
            tasks._DEFERRED_LOOKUP_DEADLINE.total_seconds(),
        )

    def test_a_batch_short_of_the_bound_still_retries(self) -> None:
        """The half that stops the tests below passing against a task that never retries."""
        bound = tasks.resolve_deferred_pin_locations.max_retries

        resent, _result = self._apply(retries=bound - 1)

        resent.assert_called_once()
        self.assertEqual(
            resent.call_args.kwargs["retries"], bound, "the last retry was not sent as retry number max_retries"
        )
        self.assertFalse(PinImportFailure.objects.filter(profile=self.profile).exists())

    def test_a_batch_at_the_bound_gives_up_and_records_failures(self) -> None:
        resent, result = self._apply(retries=tasks.resolve_deferred_pin_locations.max_retries)

        resent.assert_not_called()
        self.assertEqual(result, {"created": 0, "exists": 0, "skipped": 1})
        failure = PinImportFailure.objects.get(profile=self.profile, cid=111)
        self.assertEqual(failure.reason, PinImportFailureReason.LOOKUP_STALLED)

    def test_a_google_rate_limited_batch_at_the_bound_gives_up(self) -> None:
        """Google's fixed wait is the shortest gap, so this is the path that reaches the bound first."""
        resent, _result = self._apply(
            retries=tasks.resolve_deferred_pin_locations.max_retries, provider=PROVIDER_GOOGLE
        )

        resent.assert_not_called()
        self.assertTrue(PinImportFailure.objects.filter(profile=self.profile, cid=111).exists())

    def test_a_failing_lookup_service_at_the_bound_is_reported_as_unavailable(self) -> None:
        resent, _result = self._apply(retries=tasks.resolve_deferred_pin_locations.max_retries, request_failed=True)

        resent.assert_not_called()
        self.assertEqual(
            PinImportFailure.objects.get(profile=self.profile, cid=111).reason, PinImportFailureReason.LOOKUP_ERROR
        )

    def test_the_user_is_told_when_the_bound_ends_the_batch(self) -> None:
        self._apply(retries=tasks.resolve_deferred_pin_locations.max_retries)

        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.profile, title="Location lookup is taking longer than expected"
            ).exists()
        )

    def test_cids_the_last_round_resolved_are_placed_not_failed(self) -> None:
        """Giving up recorded a failure for every cid in the batch, including those the same round had resolved."""
        self.deferred_lists[0]["pins"].append(
            {"name": "Fort Getty", "lat": 41.49, "lng": -71.39, "description": "", "cid": 222}
        )
        partial = CidResolutionResult(provider=PROVIDER_REDATA, resolved={111: (41.348754, -71.453896)}, pending=[222])

        _resent, result = self._apply(retries=tasks.resolve_deferred_pin_locations.max_retries, lookup=partial)

        self.assertEqual(result["created"], 1)
        self.assertTrue(Pin.objects.filter(profile=self.profile).exists(), "the resolved cid was not placed")
        self.assertFalse(
            PinImportFailure.objects.filter(profile=self.profile, cid=111).exists(),
            "a placed cid was recorded as a failure",
        )
        self.assertEqual(
            PinImportFailure.objects.get(profile=self.profile, cid=222).reason, PinImportFailureReason.LOOKUP_STALLED
        )
