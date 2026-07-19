"""Tests for the Tier 1 ArcGIS REST / Socrata gateway.

Covers the query-building/response-parsing logic; live HTTP is always
mocked. The ``RealCapturedFixture*`` tests replay actual responses from a
real Socrata dataset (New Orleans' Parcels resource, see
``fixtures/property_records/``) for the specific bug they guard against: the
original ``$order=distance_in_meters(...)`` clause fails the *entire* query
outright on real Socrata infrastructure with a 400, not just the ordering -
confirmed live, not hypothetical.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.arcgis_socrata import GEOMETRY_KEY, ArcGisSocrataGateway, _esri_rings_to_dict
from urbanlens.dashboard.services.gateway import GatewayRequestError

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "property_records"


def _load_fixture(name: str) -> dict | list:
    return json.loads((_FIXTURES_DIR / name).read_text())


def _mock_response(body, *, status_code: int = 200) -> mock.Mock:
    response = mock.Mock()
    response.status_code = status_code
    response.ok = status_code < 400
    response.json.return_value = body
    return response


class QuerySocrataByPointTests(SimpleTestCase):
    def test_geo_field_required(self) -> None:
        gateway = ArcGisSocrataGateway()
        with self.assertRaises(GatewayRequestError):
            gateway.query_socrata_by_point("https://data.example.gov/resource/abcd-1234.json", "", 42.0, -73.0)

    def test_query_never_includes_an_order_clause(self) -> None:
        """Regression guard: distance_in_meters() isn't supported on every real Socrata backend and
        fails the whole query (see RealCapturedFixtureTests below) - the point-radius filter alone
        already bounds results tightly enough that ordering isn't worth the risk."""
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response([])

        gateway.query_socrata_by_point("https://data.example.gov/resource/abcd-1234.json", "the_geom", 42.0, -73.0)

        called_params = gateway.session.get.call_args.kwargs["params"]
        self.assertNotIn("$order", called_params)
        self.assertIn("$where", called_params)
        self.assertIn("within_circle", called_params["$where"])

    def test_rows_are_returned(self) -> None:
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response([{"apn": "1"}])

        rows = gateway.query_socrata_by_point("https://data.example.gov/resource/abcd-1234.json", "the_geom", 42.0, -73.0)
        self.assertEqual(rows, [{"apn": "1"}])

    def test_client_error_status_degrades_to_empty_list_not_an_exception(self) -> None:
        """A malformed query (like the real distance_in_meters case) must degrade gracefully,
        not raise - a bad query is a code bug to fix, not a reason to crash the whole pipeline."""
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response({"message": "bad query"}, status_code=400)

        rows = gateway.query_socrata_by_point("https://data.example.gov/resource/abcd-1234.json", "the_geom", 42.0, -73.0)
        self.assertEqual(rows, [])

    def test_non_list_response_is_treated_as_no_data(self) -> None:
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response({"unexpected": "shape"})

        rows = gateway.query_socrata_by_point("https://data.example.gov/resource/abcd-1234.json", "the_geom", 42.0, -73.0)
        self.assertEqual(rows, [])


class QueryArcgisByPointTests(SimpleTestCase):
    def test_requests_geometry_reprojected_to_wgs84(self) -> None:
        """Requesting outSR=4326 lets the ArcGIS server do the reprojection - the layer's own
        native spatial reference (often a local state plane or Web Mercator) is never our
        problem to invert."""
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response({"features": []})

        gateway.query_arcgis_by_point("https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/0", 39.0, -82.0)

        called_params = gateway.session.get.call_args.kwargs["params"]
        self.assertEqual(called_params["returnGeometry"], "true")
        self.assertEqual(called_params["outSR"], 4326)

    def test_geometry_is_attached_under_the_sentinel_key(self) -> None:
        body = {
            "features": [
                {
                    "attributes": {"OWNER": "Jane Smith"},
                    "geometry": {"rings": [[[-82.0, 39.0], [-82.0, 39.1], [-81.9, 39.1], [-82.0, 39.0]]], "spatialReference": {"wkid": 4326}},
                },
            ],
        }
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response(body)

        rows = gateway.query_arcgis_by_point("https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/0", 39.0, -82.0)

        self.assertEqual(rows[0]["OWNER"], "Jane Smith")
        self.assertEqual(rows[0][GEOMETRY_KEY]["format"], "esri_rings")

    def test_missing_geometry_does_not_add_the_sentinel_key(self) -> None:
        body = {"features": [{"attributes": {"OWNER": "Jane Smith"}, "geometry": None}]}
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response(body)

        rows = gateway.query_arcgis_by_point("https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/0", 39.0, -82.0)

        self.assertNotIn(GEOMETRY_KEY, rows[0])

    def test_error_response_is_treated_as_no_data(self) -> None:
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response({"error": {"code": 400, "message": "bad request"}})

        rows = gateway.query_arcgis_by_point("https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/0", 39.0, -82.0)
        self.assertEqual(rows, [])


class EsriRingsToDictTests(SimpleTestCase):
    def test_valid_polygon_geometry_is_converted(self) -> None:
        geometry = {"rings": [[[-82.0, 39.0], [-82.0, 39.1], [-81.9, 39.1], [-82.0, 39.0]]], "spatialReference": {"wkid": 4326}}
        result = _esri_rings_to_dict(geometry)
        assert result is not None
        self.assertEqual(result["format"], "esri_rings")
        self.assertEqual(result["spatial_reference"], "EPSG:4326")
        self.assertEqual(result["rings"], [[[-82.0, 39.0], [-82.0, 39.1], [-81.9, 39.1], [-82.0, 39.0]]])

    def test_none_geometry_returns_none(self) -> None:
        self.assertIsNone(_esri_rings_to_dict(None))

    def test_non_dict_geometry_returns_none(self) -> None:
        self.assertIsNone(_esri_rings_to_dict("not a dict"))

    def test_missing_rings_key_returns_none(self) -> None:
        self.assertIsNone(_esri_rings_to_dict({"spatialReference": {"wkid": 4326}}))

    def test_empty_rings_list_returns_none(self) -> None:
        self.assertIsNone(_esri_rings_to_dict({"rings": []}))

    def test_ring_with_fewer_than_three_points_is_dropped(self) -> None:
        geometry = {"rings": [[[-82.0, 39.0], [-82.0, 39.1]]]}
        self.assertIsNone(_esri_rings_to_dict(geometry))

    def test_non_numeric_coordinates_do_not_raise(self) -> None:
        geometry = {"rings": [[["not-a-number", 39.0], [-82.0, 39.1], [-81.9, 39.1]]]}
        result = _esri_rings_to_dict(geometry)
        # The malformed point is dropped, leaving too few points for a valid ring.
        self.assertIsNone(result)

    def test_multiple_rings_are_all_kept(self) -> None:
        geometry = {"rings": [[[-82.0, 39.0], [-82.0, 39.1], [-81.9, 39.1]], [[-80.0, 38.0], [-80.0, 38.1], [-79.9, 38.1]]]}
        result = _esri_rings_to_dict(geometry)
        assert result is not None
        self.assertEqual(len(result["rings"]), 2)


class RealCapturedFixtureTests(SimpleTestCase):
    """Replays real responses from New Orleans' live Parcels Socrata resource."""

    def test_the_distance_in_meters_query_really_did_fail_with_a_400(self) -> None:
        """Documents the actual live bug this fix addresses - a real Socrata backend's genuine
        error body for the query shape the gateway used to send."""
        body = _load_fixture("nola_parcels_distance_in_meters_400_response.json")
        self.assertEqual(body.get("errorCode"), "query.soql.no-such-function")
        self.assertEqual(body.get("data", {}).get("function"), "distance_in_meters")

    def test_the_working_query_shape_parses_real_rows(self) -> None:
        gateway = ArcGisSocrataGateway()
        gateway.session = mock.Mock()
        gateway.session.get.return_value = _mock_response(_load_fixture("nola_parcels_within_circle_response.json"))

        rows = gateway.query_socrata_by_point("https://data.nola.gov/resource/v9q5-fz7t.json", "the_geom", 29.9584, -90.0644)
        self.assertTrue(rows)
        self.assertIn("situs_street", rows[0])
        self.assertIn("geopin", rows[0])
