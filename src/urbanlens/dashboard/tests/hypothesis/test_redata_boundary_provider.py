"""Tests for esri_rings_to_polygon and RedataBoundaryProvider.

esri_rings_to_polygon converts REData's raw Esri ring-list geometry
(parcel_geometry/building_geometry) into GEOS polygons; RedataBoundaryProvider
wraps that conversion behind the BoundaryProvider interface the rest of the
boundary-provider chain (services.locations.boundaries) already uses.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Point, Polygon

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.base import esri_rings_to_polygon
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.apis.property_records.redata_gateway import REASON_SOURCE_ERROR, PropertyRecordsUnavailableError
from urbanlens.UrbanLens.settings.app import settings

# A clockwise square (exterior shell) around the origin.
_SQUARE_CW = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0], [0.0, 0.0]]
# A small counter-clockwise square inside it (a hole).
_HOLE_CCW = [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0], [4.0, 4.0]]
# A second, disjoint clockwise square (a separate shell).
_SQUARE_CW_2 = [[20.0, 20.0], [20.0, 30.0], [30.0, 30.0], [30.0, 20.0], [20.0, 20.0]]


class EsriRingsToPolygonTests(SimpleTestCase):
    def test_none_geometry_returns_none(self) -> None:
        self.assertIsNone(esri_rings_to_polygon(None))

    def test_wrong_format_returns_none(self) -> None:
        self.assertIsNone(esri_rings_to_polygon({"format": "geojson", "rings": [_SQUARE_CW]}))

    def test_missing_rings_returns_none(self) -> None:
        self.assertIsNone(esri_rings_to_polygon({"format": "esri_rings"}))

    def test_empty_rings_returns_none(self) -> None:
        self.assertIsNone(esri_rings_to_polygon({"format": "esri_rings", "rings": []}))

    def test_single_clockwise_ring_becomes_a_polygon(self) -> None:
        result = esri_rings_to_polygon({"format": "esri_rings", "rings": [_SQUARE_CW]})
        assert isinstance(result, Polygon)
        self.assertTrue(result.valid)
        self.assertTrue(result.contains(Point(5.0, 5.0, srid=4326)))

    def test_only_counterclockwise_rings_yields_no_shell(self) -> None:
        """A geometry with only holes and no exterior shell has nothing to attach them to."""
        self.assertIsNone(esri_rings_to_polygon({"format": "esri_rings", "rings": [_HOLE_CCW]}))

    def test_hole_is_carved_out_of_its_containing_shell(self) -> None:
        result = esri_rings_to_polygon({"format": "esri_rings", "rings": [_SQUARE_CW, _HOLE_CCW]})
        assert isinstance(result, Polygon)
        self.assertTrue(result.contains(Point(1.0, 1.0, srid=4326)))
        self.assertFalse(result.contains(Point(5.0, 5.0, srid=4326)))

    def test_hole_order_does_not_matter(self) -> None:
        """Esri doesn't guarantee a hole immediately follows its shell in the array."""
        result = esri_rings_to_polygon({"format": "esri_rings", "rings": [_HOLE_CCW, _SQUARE_CW]})
        assert isinstance(result, Polygon)
        self.assertFalse(result.contains(Point(5.0, 5.0, srid=4326)))

    def test_two_disjoint_shells_become_a_multipolygon(self) -> None:
        result = esri_rings_to_polygon({"format": "esri_rings", "rings": [_SQUARE_CW, _SQUARE_CW_2]})
        assert isinstance(result, MultiPolygon)
        self.assertEqual(len(result), 2)

    def test_malformed_points_are_skipped_not_fatal(self) -> None:
        ring = [[0.0, 0.0], ["not-a-number", "also-not"], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0], [0.0, 0.0]]
        result = esri_rings_to_polygon({"format": "esri_rings", "rings": [ring]})
        assert isinstance(result, Polygon)

    def test_ring_with_too_few_points_is_skipped(self) -> None:
        self.assertIsNone(esri_rings_to_polygon({"format": "esri_rings", "rings": [[[0.0, 0.0], [1.0, 1.0]]]}))

    def test_non_list_ring_entry_is_skipped(self) -> None:
        result = esri_rings_to_polygon({"format": "esri_rings", "rings": [_SQUARE_CW, "not-a-ring"]})
        assert isinstance(result, Polygon)


class RedataBoundaryProviderNotConfiguredTests(SimpleTestCase):
    def test_missing_url_and_key_returns_empty_dict(self) -> None:
        with mock.patch.object(settings, "redata_api_url", None), mock.patch.object(settings, "redata_api_key", None):
            result = RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)
        self.assertEqual(result, {})

    def test_missing_key_only_returns_empty_dict(self) -> None:
        with mock.patch.object(settings, "redata_api_url", "https://redata.example.test"), mock.patch.object(settings, "redata_api_key", None):
            result = RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)
        self.assertEqual(result, {})


class RedataBoundaryProviderConfiguredTests(SimpleTestCase):
    """RedataGateway itself is mocked wholesale (not just settings) - its own base_url/api_key
    fields default from settings.app at *import* time, not per-instantiation, so patching
    settings alone can't make a real construction pick up a fake key (see redata_gateway.py's
    dataclass field defaults)."""

    _GATEWAY_CLASS_PATH = "urbanlens.dashboard.services.apis.locations.boundaries.redata.RedataGateway"

    def setUp(self) -> None:
        super().setUp()
        patcher_url = mock.patch.object(settings, "redata_api_url", "https://redata.example.test")
        patcher_key = mock.patch.object(settings, "redata_api_key", "test-key")
        patcher_url.start()
        patcher_key.start()
        self.addCleanup(patcher_url.stop)
        self.addCleanup(patcher_key.stop)

    def test_unavailable_record_returns_none_for_both_kinds(self) -> None:
        with mock.patch(self._GATEWAY_CLASS_PATH) as gw_cls:
            gw_cls.return_value.lookup_parcel.side_effect = PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "down")
            result = RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)
        self.assertEqual(result, {"property": None, "building": None})

    def test_parcel_geometry_only_fills_the_property_slot(self) -> None:
        with mock.patch(self._GATEWAY_CLASS_PATH) as gw_cls:
            gw_cls.return_value.lookup_parcel.return_value = {"parcel_geometry": {"format": "esri_rings", "rings": [_SQUARE_CW]}}
            result = RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)
        self.assertIsInstance(result["property"], Polygon)
        self.assertIsNone(result["building"])

    def test_both_geometries_fill_both_slots(self) -> None:
        building_ring = [[4.0, 4.0], [4.0, 6.0], [6.0, 6.0], [6.0, 4.0], [4.0, 4.0]]
        with mock.patch(self._GATEWAY_CLASS_PATH) as gw_cls:
            gw_cls.return_value.lookup_parcel.return_value = {
                "parcel_geometry": {"format": "esri_rings", "rings": [_SQUARE_CW]},
                "building_geometry": {"format": "esri_rings", "rings": [building_ring]},
            }
            result = RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)
        self.assertIsInstance(result["property"], Polygon)
        self.assertIsInstance(result["building"], Polygon)

    def test_lookup_is_called_with_the_given_coordinates(self) -> None:
        with mock.patch(self._GATEWAY_CLASS_PATH) as gw_cls:
            gw_cls.return_value.lookup_parcel.return_value = {}
            RedataBoundaryProvider().get_typed_boundaries(42.65, -73.75)
        gw_cls.return_value.lookup_parcel.assert_called_once_with(42.65, -73.75)

    def test_get_boundary_reduces_a_multipolygon_to_its_largest_shell(self) -> None:
        with mock.patch(self._GATEWAY_CLASS_PATH) as gw_cls:
            gw_cls.return_value.lookup_parcel.return_value = {"parcel_geometry": {"format": "esri_rings", "rings": [_SQUARE_CW, _SQUARE_CW_2]}}
            result = RedataBoundaryProvider().get_boundary(42.65, -73.75)
        self.assertIsInstance(result, Polygon)

    def test_get_boundary_returns_none_when_nothing_found(self) -> None:
        with mock.patch(self._GATEWAY_CLASS_PATH) as gw_cls:
            gw_cls.return_value.lookup_parcel.return_value = {}
            result = RedataBoundaryProvider().get_boundary(42.65, -73.75)
        self.assertIsNone(result)
