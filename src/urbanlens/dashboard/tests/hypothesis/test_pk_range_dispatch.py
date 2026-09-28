"""Range dispatch covers every row once, pages by key, and drives the ranged achievement backfill."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.achievements.model import Achievement, UserAchievement
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.achievements.evaluate import evaluate_achievement_in_range
from urbanlens.dashboard.services.core.celery import dispatch_pk_ranges, pk_ranges


class PkRangesTests(TestCase):
    def setUp(self) -> None:
        baker.make(User, _quantity=7)

    def test_every_row_falls_in_exactly_one_range(self) -> None:
        pks = list(Profile.objects.order_by("pk").values_list("pk", flat=True))

        ranges = list(pk_ranges(Profile.objects.all(), 3))

        covered = [pk for first, last in ranges for pk in pks if first <= pk <= last]
        self.assertEqual(covered, pks)
        self.assertTrue(all(len([pk for pk in pks if first <= pk <= last]) <= 3 for first, last in ranges))

    def test_the_querysets_filter_decides_which_keys_count(self) -> None:
        chosen = list(Profile.objects.order_by("pk").values_list("pk", flat=True))[1::2]

        ranges = list(pk_ranges(Profile.objects.filter(pk__in=chosen), 100))

        self.assertEqual(ranges, [(chosen[0], chosen[-1])])

    def test_it_reads_one_chunk_of_keys_per_query(self) -> None:
        with CaptureQueriesContext(connection) as queries:
            ranges = list(pk_ranges(Profile.objects.all(), 3))

        self.assertEqual(len(queries), len(ranges) + 1)
        self.assertTrue(all("LIMIT 3" in query["sql"] for query in queries))

    def test_dispatch_passes_leading_arguments_before_the_bounds(self) -> None:
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            dispatched = dispatch_pk_ranges(Profile.objects.all(), tasks.backfill_achievement_range, 42, chunk_size=100)

        self.assertEqual(dispatched, 1)
        task, achievement_id, first, last = enqueue.call_args.args
        self.assertEqual((task, achievement_id), (tasks.backfill_achievement_range, 42))
        self.assertLessEqual(first, last)


class RangedBackfillTests(TestCase):
    def setUp(self) -> None:
        self.achievement = Achievement.objects.create(name="Trio", metric="pins_created", threshold=3)

    def test_the_backfill_task_only_dispatches_ranges(self) -> None:
        baker.make(User, _quantity=2)
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            dispatched = tasks.backfill_achievement(self.achievement.pk)

        self.assertGreaterEqual(dispatched, 1)
        self.assertTrue(all(call.args[0] is tasks.backfill_achievement_range for call in enqueue.call_args_list))

    def test_a_range_grants_only_to_qualifiers_without_the_award(self) -> None:
        qualifier = baker.make(User).profile
        baker.make(Pin, profile=qualifier, _quantity=3)
        holder = baker.make(User).profile
        baker.make(Pin, profile=holder, _quantity=3)
        UserAchievement.objects.create(profile=holder, achievement=self.achievement, value_at_award=3)
        baker.make(User)
        bounds = Profile.objects.order_by("pk")

        granted = evaluate_achievement_in_range(self.achievement, bounds.first().pk, bounds.last().pk)

        self.assertEqual(granted, 1)
        self.assertTrue(UserAchievement.objects.filter(profile=qualifier, achievement=self.achievement).exists())

    def test_a_ranges_queries_do_not_grow_with_its_profiles(self) -> None:
        def evaluation_queries(extra_profiles: int) -> int:
            baker.make(User, _quantity=extra_profiles)
            bounds = Profile.objects.order_by("pk")
            with CaptureQueriesContext(connection) as queries:
                evaluate_achievement_in_range(self.achievement, bounds.first().pk, bounds.last().pk)
            return len(queries)

        self.assertEqual(evaluation_queries(2), evaluation_queries(6))

    def test_an_inactive_award_is_not_backfilled(self) -> None:
        Achievement.objects.filter(pk=self.achievement.pk).update(is_active=False)
        self.achievement.refresh_from_db()

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(tasks.backfill_achievement(self.achievement.pk), 0)

        enqueue.assert_not_called()


class ReputationSweepDispatchTests(TestCase):
    def test_unscored_rows_are_dispatched_as_ranges_and_scored_rows_are_not(self) -> None:
        from model_bakery import seq

        from urbanlens.dashboard.models.reputation.model import ReputationEvent

        profile = baker.make(User).profile
        unscored = baker.make(
            ReputationEvent, profile=profile, value=None, retracted=False, target_id=seq(1), _quantity=5
        )
        baker.make(ReputationEvent, profile=profile, value=1, retracted=False, target_id=seq(100), _quantity=2)

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            tasks.sweep_reputation(chunk_size=2)

        ranges = [call.args[1:] for call in enqueue.call_args_list if call.args[0] is tasks.sweep_reputation_range]
        self.assertEqual(len(ranges), 3)
        covered = sorted(event.pk for event in unscored if any(first <= event.pk <= last for first, last in ranges))
        self.assertEqual(covered, sorted(event.pk for event in unscored))
