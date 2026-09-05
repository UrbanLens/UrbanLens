"""Longitude arithmetic must survive the antimeridian, everywhere it is done.

Three places computed with longitude as if it were an ordinary number, and each
was written independently:

- fact POINT evidence averaged to a centroid;
- the profile's saved map centre;
- the import-failure location guess, comparing a candidate against a hint.

Averaging 179.99 and -179.99 gives 0.0 - the Atlantic, 20,000km from either - and
``abs(179.99 - -179.99)`` is 359.98, so two points a kilometre apart read as
being on opposite sides of the planet. The shared helpers fix both, and return
identical answers to the naive arithmetic everywhere else, which is what the
"ordinary longitudes" tests here pin: a fix that shifted the rest of the planet
would be far worse than the bug.
"""

from __future__ import annotations

import pytest

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.geo.longitude import circular_mean_longitude, longitude_delta, normalize_longitude

_lng = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False)


class CircularMeanLongitudeTests(SimpleTestCase):
    def test_ordinary_longitudes_match_the_arithmetic_mean(self) -> None:
        """The whole planet except the date line must be unaffected."""
        self.assertAlmostEqual(circular_mean_longitude([-71.45, -71.55]), -71.50, places=6)

    def test_two_points_across_the_date_line_average_between_them(self) -> None:
        self.assertAlmostEqual(abs(circular_mean_longitude([179.99, -179.99])), 180.0, places=4)

    def test_weights_pull_the_mean_toward_the_heavier_side(self) -> None:
        mean = circular_mean_longitude([179.0, -179.0], [3.0, 1.0])

        self.assertGreater(mean, 179.0)
        self.assertLessEqual(mean, 180.0)

    def test_a_single_longitude_is_returned_unchanged(self) -> None:
        self.assertAlmostEqual(circular_mean_longitude([-179.5]), -179.5, places=6)

    def test_zero_total_weight_falls_back_to_equal_weighting(self) -> None:
        self.assertAlmostEqual(circular_mean_longitude([10.0, 20.0], [0.0, 0.0]), 15.0, places=6)

    def test_antipodal_longitudes_return_an_observed_value(self) -> None:
        """They cancel exactly; inventing a midpoint would be worse than picking one."""
        self.assertIn(circular_mean_longitude([0.0, 180.0]), (0.0, 180.0, -180.0))

    def test_an_empty_input_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one longitude"):
            circular_mean_longitude([])

    @given(lngs=st.lists(_lng, min_size=1, max_size=8))
    @settings(max_examples=50, deadline=None)
    def test_the_mean_is_always_a_valid_longitude(self, lngs: list[float]) -> None:
        self.assertGreaterEqual(circular_mean_longitude(lngs), -180.0)
        self.assertLessEqual(circular_mean_longitude(lngs), 180.0)


class LongitudeDeltaTests(SimpleTestCase):
    def test_ordinary_separations_are_the_plain_difference(self) -> None:
        self.assertAlmostEqual(longitude_delta(-71.45, -71.35), 0.10, places=6)

    def test_the_date_line_is_not_a_barrier(self) -> None:
        self.assertAlmostEqual(longitude_delta(179.99, -179.99), 0.02, places=6)

    def test_the_far_side_of_the_planet_is_180(self) -> None:
        self.assertAlmostEqual(longitude_delta(0.0, 180.0), 180.0, places=6)

    def test_it_is_symmetric(self) -> None:
        self.assertAlmostEqual(longitude_delta(179.0, -179.0), longitude_delta(-179.0, 179.0), places=9)

    @given(a=_lng, b=_lng)
    @settings(max_examples=50, deadline=None)
    def test_the_delta_never_exceeds_half_the_planet(self, a: float, b: float) -> None:
        delta = longitude_delta(a, b)

        self.assertGreaterEqual(delta, 0.0)
        self.assertLessEqual(delta, 180.0)


class NormalizeLongitudeTests(SimpleTestCase):
    """Folding must depend on the meridian, not on how the client spelled it."""

    def test_ordinary_longitudes_are_unchanged(self) -> None:
        self.assertAlmostEqual(normalize_longitude(-71.45), -71.45, places=9)

    def test_an_unwrapped_east_edge_folds(self) -> None:
        """Leaflet's getEast() returns 181 after panning across the line."""
        self.assertAlmostEqual(normalize_longitude(181.0), -179.0, places=9)

    def test_a_bound_on_the_line_keeps_its_side(self) -> None:
        self.assertEqual(normalize_longitude(180.0), 180.0)
        self.assertEqual(normalize_longitude(-180.0), -180.0)

    def test_the_same_meridian_normalises_the_same_way_however_it_was_wrapped(self) -> None:
        """180 and 540 are one meridian. Special-casing the literal 180 made them
        fold to +180 and -180 respectively, so equality checks on normalised
        values disagreed about a single line on the globe."""
        self.assertEqual(normalize_longitude(540.0), normalize_longitude(180.0))
        self.assertEqual(normalize_longitude(-540.0), normalize_longitude(-180.0))

    def test_it_is_idempotent(self) -> None:
        for value in (0.0, 179.0, 180.0, -180.0, 181.0, 540.0, -540.0):
            with self.subTest(value=value):
                once = normalize_longitude(value)

                self.assertEqual(normalize_longitude(once), once)

    @given(lng=st.floats(min_value=-1080.0, max_value=1080.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100, deadline=None)
    def test_the_result_is_always_a_valid_longitude(self, lng: float) -> None:
        folded = normalize_longitude(lng)

        self.assertGreaterEqual(folded, -180.0)
        self.assertLessEqual(folded, 180.0)
