"""Web Mercator ground resolution, and the deepest zoom a source's own resolution supports (P232)."""

from __future__ import annotations

import math

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.geo.web_mercator import meters_per_pixel, native_zoom

_HRSH_LATITUDE = 41.72


class MetersPerPixelTests(SimpleTestCase):
    def test_zoom_zero_at_the_equator_is_the_standard_constant(self) -> None:
        self.assertAlmostEqual(meters_per_pixel(0.0, 0), 156_543.03392, places=4)

    def test_each_zoom_halves_it(self) -> None:
        self.assertAlmostEqual(meters_per_pixel(_HRSH_LATITUDE, 14), meters_per_pixel(_HRSH_LATITUDE, 13) / 2)

    def test_it_shrinks_with_the_cosine_of_latitude(self) -> None:
        self.assertAlmostEqual(meters_per_pixel(60.0, 10), meters_per_pixel(0.0, 10) / 2, places=6)


class NativeZoomTests(SimpleTestCase):
    def test_sentinel_2_at_hrsh_is_zoom_13(self) -> None:
        """10 m imagery: z13 is 14.3 m a pixel there, z14 7.1 m, finer than the source."""
        self.assertEqual(native_zoom(10.0, _HRSH_LATITUDE), 13)

    def test_sub_metre_imagery_reaches_a_street_zoom(self) -> None:
        self.assertEqual(native_zoom(0.3, _HRSH_LATITUDE), 18)

    def test_a_resolution_exactly_at_a_zooms_scale_takes_that_zoom(self) -> None:
        self.assertEqual(native_zoom(meters_per_pixel(_HRSH_LATITUDE, 15), _HRSH_LATITUDE), 15)

    def test_higher_latitudes_need_a_shallower_zoom_for_the_same_source(self) -> None:
        self.assertEqual(native_zoom(10.0, 0.0), 13)
        self.assertEqual(native_zoom(10.0, 65.0), 12)

    def test_it_stays_within_the_slippy_map_range(self) -> None:
        self.assertEqual(native_zoom(1_000_000.0, 0.0), 0)
        self.assertEqual(native_zoom(0.001, 0.0), 22)

    @given(st.floats(min_value=0.05, max_value=50_000.0), st.floats(min_value=-85.0, max_value=85.0))
    def test_the_chosen_zoom_is_never_finer_than_the_source_and_the_next_one_is(
        self, resolution: float, latitude: float
    ) -> None:
        zoom = native_zoom(resolution, latitude)
        if 0 < zoom < 22:
            self.assertGreaterEqual(meters_per_pixel(latitude, zoom) * (1 + 1e-9), resolution)
            self.assertLess(meters_per_pixel(latitude, zoom + 1), resolution)

    def test_an_unusable_resolution_raises(self) -> None:
        for resolution in (0.0, -1.0, math.nan, math.inf):
            with self.subTest(resolution=resolution), self.assertRaises(ValueError):
                native_zoom(resolution, 0.0)
