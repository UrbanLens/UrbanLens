"""`parse_multipolygon_geojson` is the one place seven features parse a drawn shape.

It had no direct test. Its `except (GEOSException, TypeError, ValueError)` did not
name `GDALException`, which is what `GEOSGeometry` actually raises for most
malformed GeoJSON - including the bare `{}` a client sends when it has nothing to
submit. Every caller treats `InvalidPolygonGeoJSONError` as "answer 400"; an
uncaught `GDALException` is a 500 instead.

The reach is wider than the boundary editor it was found through:

- `controllers/boundary.py` (both the pin and the wiki editor)
- `external_api/views_wiki.py` and `external_api/serializers.py` - the public API
- `controllers/pin_lists.py` smart boundaries, `forms/search.py` saved-filter
  regions, `services/search/filter_criteria.py`, and KML/JSON import

`GDALException` is not a subclass of `GEOSException`, so no amount of catching
the latter covers it.
"""

from __future__ import annotations

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.geo.geo import (
    EmptyPolygonGeometryError,
    GeoJSONParseError,
    InvalidPolygonGeoJSONError,
    NotPolygonalGeometryError,
    parse_multipolygon_geojson,
)

_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[-74.0, 40.0], [-74.0, 40.1], [-73.9, 40.1], [-73.9, 40.0], [-74.0, 40.0]]],
}

#: Every shape a client can send that is not a usable polygon, paired with the
#: specific subclass it must raise. The first four raise GDALException rather
#: than GEOSException; `coordinates: []` parses cleanly into an *empty*
#: geometry; the rest parse but fail the polygonal check.
MALFORMED: list[tuple[dict, type[InvalidPolygonGeoJSONError]]] = [
    ({}, GeoJSONParseError),
    ({"type": "Nonsense"}, GeoJSONParseError),
    ({"type": "Polygon"}, GeoJSONParseError),
    ({"type": "Polygon", "coordinates": "nope"}, GeoJSONParseError),
    ({"type": "Polygon", "coordinates": []}, EmptyPolygonGeometryError),
    ({"type": "Point", "coordinates": [1, 2]}, NotPolygonalGeometryError),
    ({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, NotPolygonalGeometryError),
    ({"type": "GeometryCollection", "geometries": []}, NotPolygonalGeometryError),
]


class MalformedGeoJSONTests(SimpleTestCase):
    """Anything unparseable must raise the error every caller already handles."""

    def test_every_malformed_shape_raises_the_handled_error(self) -> None:
        for payload, _expected in MALFORMED:
            with self.subTest(payload=payload), pytest.raises(InvalidPolygonGeoJSONError):
                parse_multipolygon_geojson(payload)

    def test_each_malformed_shape_raises_its_specific_subclass(self) -> None:
        """Catch sites dispatch on exception type now, not message text - so the
        type raised for each failure mode must be exactly right, not just "some
        InvalidPolygonGeoJSONError".
        """
        for payload, expected in MALFORMED:
            with self.subTest(payload=payload), pytest.raises(expected):
                parse_multipolygon_geojson(payload)


class WellFormedGeoJSONTests(SimpleTestCase):
    """Anti-vacuity: a real drawing must still parse."""

    def test_a_polygon_is_coerced_to_a_multipolygon(self) -> None:
        geom = parse_multipolygon_geojson(_SQUARE)
        self.assertEqual(geom.geom_type, "MultiPolygon")
        self.assertEqual(geom.srid, 4326)
        self.assertEqual(len(geom), 1)

    def test_a_multipolygon_passes_through(self) -> None:
        geom = parse_multipolygon_geojson({"type": "MultiPolygon", "coordinates": [_SQUARE["coordinates"]]})
        self.assertEqual(geom.geom_type, "MultiPolygon")
        self.assertEqual(len(geom), 1)
