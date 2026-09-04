"""Tests for GeoBoundary - the generalized replacement for the old "USA only" bool flag.

Covers the lazy-load/memoization contract (a boundary's geometry loader must
never run more than once per instance, and never at construction time), the
bbox/WKT factories, and ``state_boundary``'s two-layer caching (Django cache
across process restarts, per-instance memoization within one).
"""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.geo.geo_boundary import _FAILED_LOAD_RETRY_SECONDS, USA, GeoBoundary, state_boundary

# A simple rectangular "state" for tests: lon in [-80, -70], lat in [40, 45].
# Esri convention is the opposite of GeoJSON's: a clockwise ring is a shell.
_SQUARE_RINGS = [[[-80.0, 40.0], [-80.0, 45.0], [-70.0, 45.0], [-70.0, 40.0], [-80.0, 40.0]]]


class FromBboxesTests(SimpleTestCase):
    def test_contains_point_inside_box(self) -> None:
        boundary = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -70.0)])
        self.assertTrue(boundary.contains(42.0, -75.0))

    def test_excludes_point_outside_box(self) -> None:
        boundary = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -70.0)])
        self.assertFalse(boundary.contains(42.0, -100.0))

    def test_none_coordinates_are_excluded(self) -> None:
        boundary = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -70.0)])
        self.assertFalse(boundary.contains(None, None))
        self.assertFalse(boundary.contains(42.0, None))

    def test_loader_runs_at_most_once(self) -> None:
        calls = []

        def _load():
            calls.append(1)
            return GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -70.0)]).geometry

        boundary = GeoBoundary(_load)
        boundary.contains(42.0, -75.0)
        boundary.contains(1.0, 1.0)
        _ = boundary.geometry

        self.assertEqual(len(calls), 1)

    def test_construction_never_invokes_loader(self) -> None:
        def _load():
            raise AssertionError("loader must not run until first real use")

        GeoBoundary(_load)  # constructing alone must not raise

    def test_loader_exception_is_swallowed_and_treated_as_unavailable(self) -> None:
        def _load():
            raise RuntimeError("upstream boom")

        boundary = GeoBoundary(_load)
        self.assertFalse(boundary.contains(42.0, -75.0))
        self.assertIsNone(boundary.geometry)

    def test_a_failed_load_is_not_retried_immediately(self) -> None:
        """Retrying on every call would hammer the provider and log per call."""
        calls = []

        def _load():
            calls.append(1)
            raise RuntimeError("upstream boom")

        boundary = GeoBoundary(_load)
        boundary.contains(42.0, -75.0)
        boundary.contains(42.0, -75.0)
        _ = boundary.geometry

        self.assertEqual(len(calls), 1)

    def test_a_failed_load_is_retried_once_the_cooldown_passes(self) -> None:
        """A boundary is typically a plugin ``ClassVar``, so memoizing a *failure*
        the way a success is memoized closes that plugin's gate for the life of
        the worker - days for a Celery process - over one transient error."""
        calls = []

        def _load():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("upstream boom")
            return GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -70.0)]).geometry

        boundary = GeoBoundary(_load)

        with patch("urbanlens.dashboard.services.geo.geo_boundary.time.monotonic", return_value=0.0):
            self.assertFalse(boundary.contains(42.0, -75.0))

        with patch(
            "urbanlens.dashboard.services.geo.geo_boundary.time.monotonic",
            return_value=_FAILED_LOAD_RETRY_SECONDS + 1.0,
        ):
            self.assertTrue(boundary.contains(42.0, -75.0), "the boundary never recovered from a transient failure")

        self.assertEqual(len(calls), 2)

    def test_a_recovered_boundary_stops_retrying(self) -> None:
        """Once it answers, it is memoized like any other success."""
        calls = []

        def _load():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("upstream boom")
            return GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -70.0)]).geometry

        boundary = GeoBoundary(_load)

        with patch("urbanlens.dashboard.services.geo.geo_boundary.time.monotonic", return_value=0.0):
            boundary.contains(42.0, -75.0)
        with patch(
            "urbanlens.dashboard.services.geo.geo_boundary.time.monotonic",
            return_value=_FAILED_LOAD_RETRY_SECONDS + 1.0,
        ):
            boundary.contains(42.0, -75.0)
            boundary.contains(42.0, -75.0)
            _ = boundary.geometry

        self.assertEqual(len(calls), 2)

    def test_a_loader_returning_none_is_memoized_permanently(self) -> None:
        """Returning None is an answer - "this boundary does not resolve" - unlike
        raising, which is the absence of one. Retrying it would re-query forever."""
        calls = []

        def _load():
            calls.append(1)

        boundary = GeoBoundary(_load)

        with patch("urbanlens.dashboard.services.geo.geo_boundary.time.monotonic", return_value=0.0):
            boundary.contains(42.0, -75.0)
        with patch(
            "urbanlens.dashboard.services.geo.geo_boundary.time.monotonic", return_value=_FAILED_LOAD_RETRY_SECONDS * 10
        ):
            boundary.contains(42.0, -75.0)

        self.assertEqual(len(calls), 1)


class FromWktTests(SimpleTestCase):
    def test_parses_polygon_and_contains_point(self) -> None:
        boundary = GeoBoundary.from_wkt("POLYGON((-80 40, -80 45, -70 45, -70 40, -80 40))")
        self.assertTrue(boundary.contains(42.0, -75.0))
        self.assertFalse(boundary.contains(0.0, 0.0))

    def test_non_polygon_wkt_resolves_to_no_geometry(self) -> None:
        """The loader's TypeError is swallowed by GeoBoundary like any other loader failure (see FromBboxesTests.test_loader_exception_is_swallowed_and_treated_as_unavailable)."""
        boundary = GeoBoundary.from_wkt("POINT(-75 42)")
        self.assertIsNone(boundary.geometry)
        self.assertFalse(boundary.contains(42.0, -75.0))


class UsaBoundaryTests(SimpleTestCase):
    def test_contains_a_conus_point(self) -> None:
        self.assertTrue(USA.contains(38.8977, -77.0365))  # Washington, DC

    def test_excludes_a_foreign_point(self) -> None:
        self.assertFalse(USA.contains(48.8566, 2.3522))  # Paris


class StateBoundaryTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.delete("geo_boundary:state:NY")

    def tearDown(self) -> None:
        cache.delete("geo_boundary:state:NY")
        super().tearDown()

    def test_contains_point_inside_fetched_polygon(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway.get_state_boundary",
            return_value={"rings": _SQUARE_RINGS},
        ) as mock_fetch:
            boundary = state_boundary("NY")
            self.assertTrue(boundary.contains(42.0, -75.0))
            mock_fetch.assert_called_once_with("NY")

    def test_excludes_point_outside_fetched_polygon(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway.get_state_boundary",
            return_value={"rings": _SQUARE_RINGS},
        ):
            boundary = state_boundary("NY")
            self.assertFalse(boundary.contains(0.0, 0.0))

    def test_second_instance_reuses_django_cache_without_refetching(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway.get_state_boundary",
            return_value={"rings": _SQUARE_RINGS},
        ) as mock_fetch:
            state_boundary("NY").contains(42.0, -75.0)
            state_boundary("NY").contains(42.0, -75.0)  # a fresh GeoBoundary instance

        mock_fetch.assert_called_once()

    def test_missing_state_resolves_to_no_geometry(self) -> None:
        with patch(
            "urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway.get_state_boundary",
            return_value=None,
        ):
            boundary = state_boundary("ZZ")
            self.assertFalse(boundary.contains(42.0, -75.0))
            self.assertIsNone(boundary.geometry)
