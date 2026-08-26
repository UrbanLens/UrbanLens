"""Tests for the OpenHistoricalMap Overpass gateway (services.apis.locations.open_historical_map).

Constructed with a fake ``session`` object throughout, which keeps
``Gateway.__post_init__`` from swapping in the real rate-limited/DB-writing
session (that swap only fires for the default ``requests.Session`` instance -
see ``Gateway.__post_init__``), so these run DB-free as ``SimpleTestCase``.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from hypothesis import given, settings, strategies as st
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.open_historical_map import (
    OhmCoverage,
    OpenHistoricalMapGateway,
    OpenHistoricalMapUnavailableError,
    _element_to_feature,
    _extract_year,
)

_HYP = {"max_examples": 100, "deadline": None}


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, *, status_code: int = 200, json_body: Any = None, json_error: Exception | None = None) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._json_body


class ExtractYearTests(SimpleTestCase):
    """_extract_year parses OHM's messy real-world date-tag values."""

    def test_plain_year(self) -> None:
        self.assertEqual(_extract_year("1888"), 1888)

    def test_year_month_day(self) -> None:
        self.assertEqual(_extract_year("2020-06-03"), 2020)

    def test_edtf_style_approximation_still_parses_leading_year(self) -> None:
        self.assertEqual(_extract_year("1849~"), 1849)

    def test_slash_range_parses_leading_year(self) -> None:
        self.assertEqual(_extract_year("1906/1908"), 1906)

    def test_non_numeric_value_is_skipped(self) -> None:
        self.assertIsNone(_extract_year("circa 1850"))

    def test_none_is_skipped(self) -> None:
        self.assertIsNone(_extract_year(None))

    def test_non_string_is_skipped(self) -> None:
        self.assertIsNone(_extract_year(1888))

    @given(year=st.integers(min_value=1, max_value=9999))
    @settings(**_HYP)
    def test_round_trips_bare_years(self, year: int) -> None:
        self.assertEqual(_extract_year(str(year)), year)


class ElementToFeatureTests(SimpleTestCase):
    """_element_to_feature converts Overpass elements into GeoJSON Features."""

    def test_node_becomes_point(self) -> None:
        feature = _element_to_feature({"type": "node", "id": 1, "lat": 40.5, "lon": -74.5, "tags": {"start_date": "1900"}})
        assert feature is not None
        self.assertEqual(feature["geometry"], {"type": "Point", "coordinates": [-74.5, 40.5]})
        self.assertEqual(feature["properties"]["id"], "node/1")
        self.assertEqual(feature["properties"]["start_date"], "1900")

    def test_open_way_becomes_linestring(self) -> None:
        geometry = [{"lat": 1.0, "lon": 2.0}, {"lat": 3.0, "lon": 4.0}]
        feature = _element_to_feature({"type": "way", "id": 2, "geometry": geometry, "tags": {}})
        assert feature is not None
        self.assertEqual(feature["geometry"]["type"], "LineString")
        self.assertEqual(feature["geometry"]["coordinates"], [[2.0, 1.0], [4.0, 3.0]])

    def test_closed_way_becomes_polygon(self) -> None:
        geometry = [
            {"lat": 0.0, "lon": 0.0},
            {"lat": 0.0, "lon": 1.0},
            {"lat": 1.0, "lon": 1.0},
            {"lat": 0.0, "lon": 0.0},
        ]
        feature = _element_to_feature({"type": "way", "id": 3, "geometry": geometry, "tags": {}})
        assert feature is not None
        self.assertEqual(feature["geometry"]["type"], "Polygon")
        self.assertEqual(feature["geometry"]["coordinates"], [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]])

    def test_relation_is_skipped(self) -> None:
        self.assertIsNone(_element_to_feature({"type": "relation", "id": 4, "tags": {}}))

    def test_way_with_missing_geometry_is_skipped(self) -> None:
        self.assertIsNone(_element_to_feature({"type": "way", "id": 5, "tags": {}}))

    def test_node_with_missing_coordinates_is_skipped(self) -> None:
        self.assertIsNone(_element_to_feature({"type": "node", "id": 6, "tags": {}}))

    def test_missing_tags_yields_empty_properties_besides_id(self) -> None:
        feature = _element_to_feature({"type": "node", "id": 7, "lat": 1.0, "lon": 2.0})
        assert feature is not None
        self.assertEqual(feature["properties"], {"id": "node/7"})


class GetCoverageTests(SimpleTestCase):
    """get_coverage() summarizes an Overpass response into an OhmCoverage."""

    def test_available_true_with_years_from_start_and_end_dates(self) -> None:
        body = {
            "elements": [
                {"type": "way", "id": 1, "tags": {"start_date": "1888", "end_date": "2020-06-03"}},
                {"type": "node", "id": 2, "tags": {"start_date": "1950"}},
            ],
        }
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body=body)
        gateway = OpenHistoricalMapGateway(session=session)

        coverage = gateway.get_coverage(40.5, -74.5)

        self.assertEqual(coverage, OhmCoverage(available=True, years=[1888, 1950, 2020]))

    def test_no_elements_is_a_normal_empty_result(self) -> None:
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body={"elements": []})
        gateway = OpenHistoricalMapGateway(session=session)

        coverage = gateway.get_coverage(40.5, -74.5)

        self.assertEqual(coverage, OhmCoverage(available=False, years=[]))

    def test_query_body_uses_around_radius_lat_lng(self) -> None:
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body={"elements": []})
        gateway = OpenHistoricalMapGateway(session=session)

        gateway.get_coverage(40.5, -74.5, radius_meters=150)

        _, kwargs = session.post.call_args
        self.assertIn("around:150,40.5,-74.5", kwargs["data"]["data"])


class GetFeaturesAtTests(SimpleTestCase):
    """get_features_at() validates its year and converts elements to GeoJSON."""

    def test_rejects_year_below_bound(self) -> None:
        gateway = OpenHistoricalMapGateway(session=mock.Mock())
        with pytest.raises(ValueError, match="year"):
            gateway.get_features_at(40.5, -74.5, 999)

    def test_rejects_year_above_bound(self) -> None:
        gateway = OpenHistoricalMapGateway(session=mock.Mock())
        with pytest.raises(ValueError, match="year"):
            gateway.get_features_at(40.5, -74.5, 2101)

    def test_returns_feature_collection(self) -> None:
        body = {"elements": [{"type": "node", "id": 1, "lat": 1.0, "lon": 2.0, "tags": {"building": "yes"}}]}
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body=body)
        gateway = OpenHistoricalMapGateway(session=session)

        result = gateway.get_features_at(40.5, -74.5, 1950)

        self.assertEqual(result["type"], "FeatureCollection")
        self.assertEqual(len(result["features"]), 1)

    def test_relations_are_excluded_from_the_collection(self) -> None:
        body = {"elements": [{"type": "relation", "id": 1, "tags": {}}]}
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body=body)
        gateway = OpenHistoricalMapGateway(session=session)

        result = gateway.get_features_at(40.5, -74.5, 1950)

        self.assertEqual(result["features"], [])

    @given(year=st.integers(min_value=1000, max_value=2100))
    @settings(**_HYP)
    def test_valid_years_never_raise(self, year: int) -> None:
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body={"elements": []})
        gateway = OpenHistoricalMapGateway(session=session)
        gateway.get_features_at(40.5, -74.5, year)


class UnavailableErrorTests(SimpleTestCase):
    """Network/timeout/malformed-response failures raise OpenHistoricalMapUnavailableError."""

    def test_connection_failure_raises_unavailable(self) -> None:
        session = mock.Mock()
        session.post.side_effect = ConnectionError("boom")
        gateway = OpenHistoricalMapGateway(session=session)

        with pytest.raises(OpenHistoricalMapUnavailableError):
            gateway.get_coverage(40.5, -74.5)

    def test_non_200_status_raises_unavailable(self) -> None:
        session = mock.Mock()
        session.post.return_value = _FakeResponse(status_code=504)
        gateway = OpenHistoricalMapGateway(session=session)

        with pytest.raises(OpenHistoricalMapUnavailableError):
            gateway.get_coverage(40.5, -74.5)

    def test_unparseable_json_raises_unavailable(self) -> None:
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_error=ValueError("not json"))
        gateway = OpenHistoricalMapGateway(session=session)

        with pytest.raises(OpenHistoricalMapUnavailableError):
            gateway.get_coverage(40.5, -74.5)

    def test_non_dict_json_raises_unavailable(self) -> None:
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body=["not", "a", "dict"])
        gateway = OpenHistoricalMapGateway(session=session)

        with pytest.raises(OpenHistoricalMapUnavailableError):
            gateway.get_coverage(40.5, -74.5)

    def test_unavailable_error_is_never_raised_for_an_empty_result(self) -> None:
        """Queried fine, nothing found -- a normal result, never this exception."""
        session = mock.Mock()
        session.post.return_value = _FakeResponse(json_body={"elements": []})
        gateway = OpenHistoricalMapGateway(session=session)

        try:
            coverage = gateway.get_coverage(40.5, -74.5)
        except OpenHistoricalMapUnavailableError:
            self.fail("An empty-but-successful query must not raise OpenHistoricalMapUnavailableError.")
        self.assertFalse(coverage.available)
