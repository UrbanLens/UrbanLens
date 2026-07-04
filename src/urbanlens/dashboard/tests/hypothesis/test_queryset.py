"""Property-based tests for PinQuerySet.filter_by_criteria.

filter_by_criteria is the primary server-side search engine.  For each filter
key the invariants are:

    Completeness  - every matching pin IS in the result.
    Soundness     - every pin in the result DOES match the criterion.
    Idempotency   - applying the same criteria twice yields the same result.
    Monotonicity  - removing a criterion never produces a smaller result set.

The pin fixtures are created once in setUp so they survive across all
@given examples (which are each wrapped in an individually rolled-back
savepoint by hypothesis.extra.django.TestCase).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.contrib.auth.models import User
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.model import KIND_TAG, Badge
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reviews.model import Review

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


class FilterByCriteriaHasVisitsTests(TestCase):
    """has_visits criterion: 'yes' returns visited; 'no' returns unvisited."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.visited = baker.make(Pin, profile=self.profile, last_visited=baker.random_gen.gen_datetime())
        self.unvisited = baker.make(Pin, profile=self.profile, last_visited=None)

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_has_visits_yes_includes_visited_pins(self) -> None:
        qs = self._base_qs().filter_by_criteria({"has_visits": "yes"})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.visited.pk, result_ids)
        self.assertNotIn(self.unvisited.pk, result_ids)

    def test_has_visits_no_includes_unvisited_pins(self) -> None:
        qs = self._base_qs().filter_by_criteria({"has_visits": "no"})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertNotIn(self.visited.pk, result_ids)
        self.assertIn(self.unvisited.pk, result_ids)

    def test_has_visits_empty_string_applies_no_filter(self) -> None:
        qs = self._base_qs().filter_by_criteria({"has_visits": ""})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.visited.pk, result_ids)
        self.assertIn(self.unvisited.pk, result_ids)

    def test_omitting_has_visits_applies_no_filter(self) -> None:
        qs = self._base_qs().filter_by_criteria({})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.visited.pk, result_ids)
        self.assertIn(self.unvisited.pk, result_ids)


class FilterByCriteriaPriorityTests(TestCase):
    """min_priority criterion: result contains only pins at or above the threshold."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        # Pins at fixed priority levels.
        self.priorities = [0, 25, 50, 75, 100]
        self.pins_by_priority: dict[int, Pin] = {
            p: baker.make(Pin, profile=self.profile, priority=p)
            for p in self.priorities
        }

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    @given(min_prio=st.sampled_from([0, 25, 50, 75, 100]))
    @_db_settings
    def test_soundness_all_results_meet_threshold(self, min_prio: int) -> None:
        qs = self._base_qs().filter_by_criteria({"min_priority": min_prio})
        for pin in qs:
            self.assertGreaterEqual(pin.priority, min_prio)

    @given(min_prio=st.sampled_from([0, 25, 50, 75, 100]))
    @_db_settings
    def test_completeness_all_qualifying_pins_present(self, min_prio: int) -> None:
        result_ids = set(self._base_qs().filter_by_criteria({"min_priority": min_prio}).values_list("pk", flat=True))
        for p, pin in self.pins_by_priority.items():
            if p >= min_prio:
                self.assertIn(pin.pk, result_ids, f"Pin with priority {p} should be in result (min={min_prio})")

    @given(min_prio=st.sampled_from([25, 50, 75, 100]))
    @_db_settings
    def test_below_threshold_pins_excluded(self, min_prio: int) -> None:
        result_ids = set(self._base_qs().filter_by_criteria({"min_priority": min_prio}).values_list("pk", flat=True))
        for p, pin in self.pins_by_priority.items():
            if p < min_prio:
                self.assertNotIn(pin.pk, result_ids, f"Pin with priority {p} must be excluded (min={min_prio})")


class FilterByCriteriaDateTests(TestCase):
    """created_after / created_before criteria."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_created_after_excludes_older_pins(self) -> None:
        future = date.today() + timedelta(days=365)
        old_pin = baker.make(Pin, profile=self.profile)
        qs = self._base_qs().filter_by_criteria({"created_after": future})
        self.assertNotIn(old_pin.pk, qs.values_list("pk", flat=True))

    def test_created_before_excludes_future_pins(self) -> None:
        past = date.today() - timedelta(days=365 * 100)
        pin = baker.make(Pin, profile=self.profile)
        qs = self._base_qs().filter_by_criteria({"created_before": past})
        self.assertNotIn(pin.pk, qs.values_list("pk", flat=True))

    def test_pins_created_today_pass_both_bounds(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        today = date.today()
        qs = self._base_qs().filter_by_criteria({
            "created_after": today,
            "created_before": today,
        })
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))


class FilterByCriteriaRatingTests(TestCase):
    """min_rating / max_rating criteria filter by review score."""

    user: User
    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile  # auto-created by post_save signal
        # One pin per rating 1-5 (rating 0 = no review).
        self.pins_by_rating: dict[int, Pin] = {}
        for rating in range(1, 6):
            pin = baker.make(Pin, profile=self.profile)
            baker.make(Review, user=self.user, pin=pin, rating=rating)
            self.pins_by_rating[rating] = pin

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    @given(min_r=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_min_rating_soundness(self, min_r: int) -> None:
        qs = self._base_qs().filter_by_criteria({"min_rating": min_r})
        for pk, rating_val in qs.values_list("pk", "reviews__rating"):
            if rating_val is not None:
                self.assertGreaterEqual(rating_val, min_r)

    @given(max_r=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_max_rating_soundness(self, max_r: int) -> None:
        qs = self._base_qs().filter_by_criteria({"max_rating": max_r})
        for pk, rating_val in qs.values_list("pk", "reviews__rating"):
            if rating_val is not None:
                self.assertLessEqual(rating_val, max_r)

    @given(
        bounds=st.tuples(st.integers(min_value=1, max_value=5), st.integers(min_value=1, max_value=5)).map(
            lambda t: (min(t), max(t)),
        ),
    )
    @_db_settings
    def test_rating_band_returns_only_pins_in_range(self, bounds: tuple[int, int]) -> None:
        min_r, max_r = bounds
        qs = self._base_qs().filter_by_criteria({"min_rating": min_r, "max_rating": max_r})
        for pk, rating_val in qs.values_list("pk", "reviews__rating"):
            if rating_val is not None:
                self.assertGreaterEqual(rating_val, min_r)
                self.assertLessEqual(rating_val, max_r)


class FilterByCriteriaNameTests(TestCase):
    """name criterion: case-insensitive substring match against name."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.target = baker.make(Pin, profile=self.profile, name="Urbex Hospital")
        self.decoy = baker.make(Pin, profile=self.profile, name="Mountain Trail")

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_exact_match_finds_target(self) -> None:
        qs = self._base_qs().filter_by_criteria({"name": "Urbex Hospital"})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.target.pk, result_ids)
        self.assertNotIn(self.decoy.pk, result_ids)

    def test_case_insensitive_match(self) -> None:
        qs = self._base_qs().filter_by_criteria({"name": "urbex hospital"})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.target.pk, result_ids)

    def test_partial_match_succeeds(self) -> None:
        qs = self._base_qs().filter_by_criteria({"name": "Hospital"})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.target.pk, result_ids)

    def test_non_matching_name_excludes_pin(self) -> None:
        qs = self._base_qs().filter_by_criteria({"name": "ZZZNotMatchingZZZ"})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertNotIn(self.target.pk, result_ids)

    def test_empty_name_string_applies_no_filter(self) -> None:
        qs = self._base_qs().filter_by_criteria({"name": ""})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.target.pk, result_ids)
        self.assertIn(self.decoy.pk, result_ids)

    def test_whitespace_only_name_applies_no_filter(self) -> None:
        qs = self._base_qs().filter_by_criteria({"name": "   "})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.target.pk, result_ids)
        self.assertIn(self.decoy.pk, result_ids)


class FilterByCriteriaIdempotencyTests(TestCase):
    """Applying the same criteria object twice must yield an identical result."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        baker.make(Pin, profile=self.profile, _quantity=5)

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    @given(
        min_prio=st.integers(min_value=0, max_value=100),
    )
    @_db_settings
    def test_double_application_same_result(self, min_prio: int) -> None:
        criteria: dict[str, Any] = {"min_priority": min_prio}
        first = set(self._base_qs().filter_by_criteria(criteria).values_list("pk", flat=True))
        second = set(self._base_qs().filter_by_criteria(criteria).values_list("pk", flat=True))
        self.assertEqual(first, second, "filter_by_criteria must be deterministic")


class FilterByCriteriaMonotonicityTests(TestCase):
    """Loosening a criterion must never produce a strictly smaller result set."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        for priority_val in range(1, 6):
            baker.make(Pin, profile=self.profile, priority=priority_val * 20)

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    @given(
        high_min=st.integers(min_value=26, max_value=100),
    )
    @_db_settings
    def test_lowering_min_priority_never_shrinks_result(self, high_min: int) -> None:
        low_min = high_min - 25
        tight_ids = set(self._base_qs().filter_by_criteria({"min_priority": high_min}).values_list("pk", flat=True))
        loose_ids = set(self._base_qs().filter_by_criteria({"min_priority": low_min}).values_list("pk", flat=True))
        self.assertTrue(
            tight_ids.issubset(loose_ids),
            f"Lowering min_priority from {high_min} to {low_min} shrank the result",
        )


class FilterByCriteriaTagTests(TestCase):
    """Tag criterion filters via Badge.get_badge_and_descendants."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.tag = baker.make(Badge, kind=KIND_TAG, profile=None, name="urban-exploration")
        self.tagged_pin = baker.make(Pin, profile=self.profile)
        self.tagged_pin.badges.add(self.tag)
        self.untagged_pin = baker.make(Pin, profile=self.profile)

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_tag_filter_includes_pins_with_tag(self) -> None:
        qs = self._base_qs().filter_by_criteria({"tags": [self.tag]})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(self.tagged_pin.pk, result_ids)

    def test_tag_filter_excludes_untagged_pins(self) -> None:
        qs = self._base_qs().filter_by_criteria({"tags": [self.tag]})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertNotIn(self.untagged_pin.pk, result_ids)

    def test_tag_filter_includes_child_tag_pins(self) -> None:
        """Pins with a child tag of the filter tag must also appear."""
        child_tag = baker.make(Badge, kind=KIND_TAG, profile=None, name="abandoned-urbex")
        child_tag.parents.add(self.tag)
        child_pin = baker.make(Pin, profile=self.profile)
        child_pin.badges.add(child_tag)
        qs = self._base_qs().filter_by_criteria({"tags": [self.tag]})
        result_ids = set(qs.values_list("pk", flat=True))
        self.assertIn(child_pin.pk, result_ids)
