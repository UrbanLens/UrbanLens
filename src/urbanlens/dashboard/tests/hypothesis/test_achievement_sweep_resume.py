"""The nightly achievement sweep must stay bounded per task invocation.

`sweep_achievements` used to evaluate every profile inside a single Celery
task, ~30 queries per profile, under the hard 3600s task time limit - so past
a certain user count the run was killed mid-iteration, always truncating the
same tail of profiles. It is now a dispatcher: profile pks are sliced into
bounded ranges and each range is evaluated by its own
`sweep_achievements_range` task with one bulk metric pass, so no invocation
can approach the time limit and a crashed chunk costs only its own range for
one night. (This replaced an earlier cursor-resume fix, whose crash-recovery
job the chunking makes redundant.)

Two properties are guarded here: dispatch partitions the profiles (every
profile lands in exactly one chunk), and a chunk's query cost is priced in
metrics, not profiles.
"""

from __future__ import annotations

import math
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.achievements.model import Achievement, UserAchievement
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.achievements.evaluate import evaluate_profiles_in_range
from urbanlens.dashboard.services.achievements.metrics import all_metrics


class SweepDispatchTests(TestCase):
    """The dispatcher slices profiles into pk ranges, one subtask per range."""

    def setUp(self) -> None:
        super().setUp()
        self.achievement = baker.make(
            "dashboard.Achievement",
            name="Sweep pin",
            metric="pins_created",
            threshold=1,
            is_active=True,
        )
        self.profiles = sorted(
            (Profile.objects.get(user=baker.make("auth.User")) for _ in range(6)),
            key=lambda profile: profile.pk,
        )
        self.pks = [profile.pk for profile in self.profiles]

    def _dispatched_ranges(self, chunk_size: int) -> tuple[int, list[tuple[int, int]]]:
        """Run the dispatcher, capturing the (start_pk, end_pk) range per subtask."""
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            dispatched = tasks.sweep_achievements(chunk_size=chunk_size)
        ranges = [
            (call.args[1], call.args[2])
            for call in enqueue.call_args_list
            if call.args and call.args[0] is tasks.sweep_achievements_range
        ]
        return dispatched, ranges

    @given(chunk_size=st.integers(min_value=1, max_value=8))
    @hypothesis_settings(max_examples=10, deadline=None)
    def test_dispatch_partitions_every_profile_into_exactly_one_chunk(self, chunk_size: int) -> None:
        """No profile may be swept twice or - the original bug - not at all."""
        dispatched, ranges = self._dispatched_ranges(chunk_size)

        self.assertEqual(dispatched, math.ceil(len(self.pks) / chunk_size))
        self.assertEqual(len(ranges), dispatched)
        for pk in self.pks:
            covering = [r for r in ranges if r[0] <= pk <= r[1]]
            self.assertEqual(len(covering), 1, f"profile {pk} must fall in exactly one chunk, got {covering}")

    def test_chunks_are_bounded_by_chunk_size(self) -> None:
        _, ranges = self._dispatched_ranges(chunk_size=4)

        for start_pk, end_pk in ranges:
            covered = [pk for pk in self.pks if start_pk <= pk <= end_pk]
            self.assertLessEqual(len(covered), 4)

    def test_no_active_achievement_dispatches_nothing(self) -> None:
        """The signals' gate, applied here too: no award defined, no fan-out."""
        Achievement.objects.all().delete()

        dispatched, ranges = self._dispatched_ranges(chunk_size=2)

        self.assertEqual(dispatched, 0)
        self.assertEqual(ranges, [])

    def test_running_every_dispatched_chunk_awards_every_qualifying_profile(self) -> None:
        """End to end: dispatch, run each range subtask, everyone is granted."""
        for profile in self.profiles:
            baker.make(Pin, profile=profile)

        _, ranges = self._dispatched_ranges(chunk_size=2)
        granted = sum(tasks.sweep_achievements_range(start_pk, end_pk) for start_pk, end_pk in ranges)

        self.assertEqual(granted, len(self.profiles))
        awarded = set(
            UserAchievement.objects.filter(achievement=self.achievement).values_list("profile_id", flat=True),
        )
        self.assertEqual(awarded, set(self.pks))

    def test_a_chunk_only_touches_its_own_range(self) -> None:
        """A crashed chunk must cost its own range and nothing else."""
        for profile in self.profiles:
            baker.make(Pin, profile=profile)

        _, ranges = self._dispatched_ranges(chunk_size=2)
        first_start, first_end = ranges[0]
        tasks.sweep_achievements_range(first_start, first_end)

        awarded = set(UserAchievement.objects.values_list("profile_id", flat=True))
        self.assertEqual(awarded, {pk for pk in self.pks if first_start <= pk <= first_end})

    def test_rerunning_a_chunk_grants_nothing_new(self) -> None:
        for profile in self.profiles:
            baker.make(Pin, profile=profile)

        _, ranges = self._dispatched_ranges(chunk_size=3)
        for start_pk, end_pk in ranges:
            tasks.sweep_achievements_range(start_pk, end_pk)

        rerun_granted = sum(tasks.sweep_achievements_range(start_pk, end_pk) for start_pk, end_pk in ranges)
        self.assertEqual(rerun_granted, 0)
        self.assertEqual(UserAchievement.objects.count(), len(self.profiles))


class SweepChunkQueryCostTests(TestCase):
    """A chunk is priced in metrics, not profiles.

    The measured cost that motivated the fix was ~30 queries per profile per
    sweep. With bulk metrics, a chunk's query count must be a function of the
    active metric count alone - the same for 4 profiles as for 12.
    """

    def setUp(self) -> None:
        super().setUp()
        # One active achievement per registered metric, mirroring the
        # docs/PROBLEMS.md measurement. Thresholds are unreachable so no
        # grant queries muddy the count.
        for metric in all_metrics():
            baker.make(
                "dashboard.Achievement",
                name=f"cost-{metric.key}",
                metric=metric.key,
                threshold=100_000,
                is_active=True,
            )
        self.pks = sorted(Profile.objects.get(user=baker.make("auth.User")).pk for _ in range(12))

    def _queries_for_range(self, start_pk: int, end_pk: int) -> int:
        with CaptureQueriesContext(connection) as context:
            evaluate_profiles_in_range(start_pk, end_pk)
        return len(context.captured_queries)

    def test_chunk_query_count_does_not_scale_with_profile_count(self) -> None:
        # Warm-up absorbs any one-time lazy work so both captures are equal.
        evaluate_profiles_in_range(self.pks[0], self.pks[-1])

        four_profiles = self._queries_for_range(self.pks[0], self.pks[3])
        twelve_profiles = self._queries_for_range(self.pks[0], self.pks[-1])

        self.assertEqual(
            four_profiles,
            twelve_profiles,
            "a chunk's query count must not depend on how many profiles it holds",
        )
        # ~30 x 12 profiles was ~360 queries; the bulk pass must land within a
        # small multiple of the metric count (a couple of metrics need two
        # grouped queries) plus fixed overhead.
        self.assertLessEqual(twelve_profiles, 2 * len(all_metrics()) + 5)
