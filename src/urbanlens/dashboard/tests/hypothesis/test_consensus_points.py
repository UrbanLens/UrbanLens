"""Tests for the Consensus leveling formula (services.consensus.points).

Pure math, no DB - see points_required_for_level's docstring for the
curve's shape rationale (logarithmic cost-density, never free, never
unreachable).
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.consensus.points import MAX_LEVEL, level_for_points, points_required_for_level

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
