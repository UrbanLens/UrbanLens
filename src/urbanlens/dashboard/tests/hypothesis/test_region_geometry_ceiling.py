"""A drawn region has to be bounded, because dissolving it is superlinear.

`dissolve_polygons` merges every pair of components that intersect, and restarts
the whole scan after each merge - so its cost is O(n^2) per pass with up to n
passes, in GEOS `intersects()` calls, inside the request that drew the region.
Nothing capped n. The count comes from a POST body: `parse_region_geojson` reads
`include_regions`/`exclude_regions` off the saved-filter form and hands whatever
parsed straight through (N21 H12).

Vertices matter as much as components. One polygon with a hundred thousand of
them is a cheap `len()` and an expensive `intersects()`, so a component cap
alone would move the same cost rather than remove it.

Both are refused as `InvalidPolygonGeoJSONError`, which every caller already
turns into a 400 - a new failure mode here should not become a 500 on the public
API and the drawing editors.
"""

from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.geo.geo import (
    MAX_REGION_POLYGONS,
    MAX_REGION_VERTICES,
    InvalidPolygonGeoJSONError,
    TooComplexGeometryError,
    parse_multipolygon_geojson,
)


def _square(x: float, y: float, size: float = 0.001) -> Polygon:
    """One small square, far enough from its neighbours not to intersect."""
    return Polygon(((x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)), srid=4326)


def _disjoint_squares(count: int) -> dict:
    """GeoJSON for *count* non-overlapping squares - the worst case for the
    dissolve, since nothing merges and every pair is compared."""
    polygons = [_square(index * 0.01, 0.0) for index in range(count)]
    return {"type": "MultiPolygon", "coordinates": [list(polygon.coords) for polygon in polygons]}


def _many_sided(vertices: int) -> dict:
    """GeoJSON for one polygon with *vertices* points around a circle."""
    import math

    ring = [
        (math.cos(2 * math.pi * index / vertices), math.sin(2 * math.pi * index / vertices))
        for index in range(vertices)
    ]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


class TheCeilingsExistTests(SimpleTestCase):
    """Before anything can be bounded, something has to name the bound."""

    def test_the_component_ceiling_is_a_number(self) -> None:
        self.assertGreater(MAX_REGION_POLYGONS, 0)

    def test_the_vertex_ceiling_is_a_number(self) -> None:
        self.assertGreater(MAX_REGION_VERTICES, 0)

    def test_a_refusal_is_the_kind_callers_already_handle(self) -> None:
        """Every caller catches InvalidPolygonGeoJSONError and answers 400. A
        new exception outside that hierarchy would be a 500 instead."""
        self.assertTrue(issubclass(TooComplexGeometryError, InvalidPolygonGeoJSONError))


class TheRegionIsBoundedTests(SimpleTestCase):
    """The rule, from both sides - a cap nothing accepts is not a cap."""

    def test_a_region_at_the_component_ceiling_is_accepted(self) -> None:
        parsed = parse_multipolygon_geojson(_disjoint_squares(MAX_REGION_POLYGONS))

        self.assertIsInstance(parsed, MultiPolygon)
        self.assertEqual(len(parsed), MAX_REGION_POLYGONS)

    def test_a_region_past_the_component_ceiling_is_refused(self) -> None:
        with self.assertRaises(TooComplexGeometryError):
            parse_multipolygon_geojson(_disjoint_squares(MAX_REGION_POLYGONS + 1))

    def test_a_polygon_past_the_vertex_ceiling_is_refused(self) -> None:
        """A component cap alone would move this cost rather than remove it."""
        with self.assertRaises(TooComplexGeometryError):
            parse_multipolygon_geojson(_many_sided(MAX_REGION_VERTICES + 2))

    def test_an_ordinary_drawn_region_is_untouched(self) -> None:
        """The ceilings are a safety net, not a feature limit: what somebody
        actually draws has to pass without thinking about them."""
        parsed = parse_multipolygon_geojson(_disjoint_squares(12))

        self.assertEqual(len(parsed), 12)
