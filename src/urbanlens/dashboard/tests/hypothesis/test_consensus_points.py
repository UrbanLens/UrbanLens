"""Tests for the Consensus leveling formula (services.consensus.points).

Pure math, no DB - see points_required_for_level's docstring for the
curve's shape rationale (logarithmic cost-density, never free, never
unreachable).
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.consensus.points import (
    MANUAL_EDIT_EXTRA_POINTS,
    MANUAL_EDIT_POINTS_CAP,
    MAX_LEVEL,
    SOLO_ANSWER_POINTS,
    level_for_points,
    points_for_changes,
    points_required_for_level,
)

_HYP = {"max_examples": 200, "deadline": None}


class PointsRequiredForLevelTests(SimpleTestCase):
    def test_level_below_one_costs_nothing(self) -> None:
        self.assertEqual(points_required_for_level(0), 0)
        self.assertEqual(points_required_for_level(-5), 0)

    def test_level_one_costs_something(self) -> None:
        self.assertGreater(points_required_for_level(1), 0)

    @given(level=st.integers(min_value=1, max_value=1000))
    @settings(**_HYP)
    def test_strictly_increasing(self, level: int) -> None:
        self.assertLess(points_required_for_level(level), points_required_for_level(level + 1))

    @given(level=st.integers(min_value=1, max_value=1000))
    @settings(**_HYP)
    def test_cost_density_grows_slower_than_linear(self, level: int) -> None:
        """threshold(n)/n must not grow as fast as n itself - i.e. leveling stays achievable at high levels."""
        density_now = points_required_for_level(level) / level
        density_later = points_required_for_level(level * 10) / (level * 10)
        self.assertLess(density_later, density_now * 10)


class LevelForPointsTests(SimpleTestCase):
    def test_zero_points_is_level_one(self) -> None:
        self.assertEqual(level_for_points(0), 1)

    def test_never_below_level_one(self) -> None:
        self.assertEqual(level_for_points(-100), 1)

    def test_never_exceeds_max_level(self) -> None:
        self.assertLessEqual(level_for_points(10**12), MAX_LEVEL)

    @given(points=st.integers(min_value=0, max_value=200_000))
    @settings(**_HYP)
    def test_monotonic_nondecreasing(self, points: int) -> None:
        self.assertGreaterEqual(level_for_points(points + 1), level_for_points(points))

    @given(level=st.integers(min_value=1, max_value=200))
    @settings(**_HYP)
    def test_round_trips_at_the_threshold(self, level: int) -> None:
        """Reaching exactly the points required for `level` puts you at (at least) that level."""
        threshold = points_required_for_level(level)
        self.assertGreaterEqual(level_for_points(threshold), level + 1)

    @given(level=st.integers(min_value=1, max_value=200))
    @settings(**_HYP)
    def test_one_point_short_of_threshold_is_not_yet_that_level(self, level: int) -> None:
        threshold = points_required_for_level(level)
        if threshold > 0:
            self.assertLess(level_for_points(threshold - 1), level + 1)


class PointsForChangesTests(SimpleTestCase):
    """Weighting one edit's diff, and the ceiling that keeps it in its place.

    The award used to be a flat 3 per edit, so a one-word alias earned what a
    rewritten description did, and a dialog submit touching every field earned
    3 per field with no ceiling.
    """

    def test_an_empty_diff_earns_nothing(self) -> None:
        self.assertEqual(points_for_changes({}), 0)
        self.assertEqual(points_for_changes(None), 0)

    def test_a_substantive_field_outweighs_an_alias(self) -> None:
        substantive = points_for_changes({"description": {"from": "a", "to": "b"}})
        cheap = points_for_changes({"alias_added": {"from": None, "to": "Old Mill"}})

        self.assertGreater(substantive, cheap)
        self.assertGreater(cheap, 0, "a cheap contribution is still a contribution")

    def test_boundary_keys_count_as_substantive(self) -> None:
        self.assertEqual(
            points_for_changes({"bounding_box": {"from": "", "to": "POLYGON EMPTY"}}),
            points_for_changes({"description": {"from": "a", "to": "b"}}),
        )

    def test_an_unrecognised_key_falls_to_the_cheaper_tier(self) -> None:
        """A new edit kind must not become the best rate in the game by being added."""
        self.assertEqual(points_for_changes({"a_field_invented_next_year": {}}), MANUAL_EDIT_EXTRA_POINTS)

    def test_a_bulk_import_cannot_out_earn_the_cap(self) -> None:
        hundred_buildings = {f"child_wiki_{index}": {} for index in range(100)}

        self.assertEqual(points_for_changes(hundred_buildings), MANUAL_EDIT_POINTS_CAP)

    @given(keys=st.lists(st.text(min_size=1, max_size=30), min_size=0, max_size=40, unique=True))
    @settings(**_HYP)
    def test_always_within_bounds(self, keys: list[str]) -> None:
        self.assertIn(points_for_changes({key: {} for key in keys}), range(MANUAL_EDIT_POINTS_CAP + 1))

    def test_the_cap_stays_below_an_in_game_answer(self) -> None:
        """The stated intent of the award, asserted rather than only commented.

        ``points.py`` says the out-of-game wiki edit is "deliberately worth less
        than any in-game path so playing the game is still the primary way to
        rack up points". A retune that lifts the cap past a solo answer would
        invert that silently.
        """
        self.assertLess(MANUAL_EDIT_POINTS_CAP, SOLO_ANSWER_POINTS)
