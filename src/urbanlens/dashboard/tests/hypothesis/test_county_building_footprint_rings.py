"""A county building footprint REData delivers in Esri ring form is used, not dropped.

REData's ``Parcel`` exposes ``parcel_geometry`` as GeoJSON, but has no top-level ``building_geometry``: the county
footprint reaches UrbanLens only inside ``record_payload``, as ``{"format": "esri_rings", "rings": [...]}``. The
boundary provider read it as GeoJSON and so never used it.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import Point, Polygon

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.base import polygon_from_wire
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
from urbanlens.UrbanLens.settings.app import settings

# Esri winds an exterior ring clockwise.
_ESRI_BUILDING = {
    "format": "esri_rings",
    "spatial_reference": "EPSG:4326",
    "rings": [[[-73.751, 42.649], [-73.751, 42.651], [-73.749, 42.651], [-73.749, 42.649], [-73.751, 42.649]]],
}
_GEOJSON_PARCEL = {
    "type": "Polygon",
    "coordinates": [[[-73.76, 42.64], [-73.74, 42.64], [-73.74, 42.66], [-73.76, 42.66], [-73.76, 42.64]]],
}


class PolygonFromWireTests(SimpleTestCase):
    def test_esri_rings_are_read(self) -> None:
        polygon = polygon_from_wire(_ESRI_BUILDING)

        assert isinstance(polygon, Polygon)
        self.assertTrue(polygon.contains(Point(-73.75, 42.65, srid=4326)))

    def test_geojson_is_read(self) -> None:
        self.assertIsInstance(polygon_from_wire(_GEOJSON_PARCEL), Polygon)

    def test_anything_else_is_nothing(self) -> None:
        for geometry in (None, {}, {"type": "Point", "coordinates": [0, 0]}, {"format": "esri_rings"}, "x"):
            with self.subTest(geometry=geometry):
                self.assertIsNone(polygon_from_wire(geometry))


class CountyFootprintTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        for attribute, value in (("redata_api_url", "https://redata.example.test"), ("redata_api_key", "test-key")):
            patcher = mock.patch.object(settings, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_footprint_inside_the_record_reaches_the_building_slot(self) -> None:
        session = mock.Mock()
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {
            "uuid": "3fae2b1c-0000-0000-0000-000000000000",
            "parcel_geometry": _GEOJSON_PARCEL,
            "record_payload": {"owner_name": ["Jane Smith"], "building_geometry": _ESRI_BUILDING},
        }
        session.get.return_value = response
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session)

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.boundaries.redata.RedataGateway", return_value=gateway
        ):
            result = RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)

        building = result["building"]
        assert isinstance(building, Polygon)
        self.assertTrue(building.contains(Point(-73.75, 42.65, srid=4326)))
        self.assertIsInstance(result["property"], Polygon)
