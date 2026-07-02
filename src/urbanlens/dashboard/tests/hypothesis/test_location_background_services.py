"""Tests for location creation and place-name resolver services."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.locations.creation import _default_bbox
from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain
from urbanlens.dashboard.services.locations.naming import is_meaningful_name


@dataclass(slots=True)
class _Resolver:
    value: str | None

    def resolve(self, latitude: float, longitude: float) -> str | None:
        return self.value


class DefaultBoundingBoxTests(TestCase):
    """Default generated bounding boxes contain the source coordinate."""

    @given(
        latitude=st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False),
        longitude=st.floats(min_value=-179.9, max_value=179.9, allow_nan=False, allow_infinity=False),
    )
    @hyp_settings(max_examples=30)
    def test_default_bbox_surrounds_coordinate(self, latitude: float, longitude: float) -> None:
        bbox = _default_bbox(latitude, longitude)
        min_x, min_y, max_x, max_y = bbox.extent
        self.assertLess(min_x, longitude)
        self.assertGreater(max_x, longitude)
        self.assertLess(min_y, latitude)
        self.assertGreater(max_y, latitude)


class PlaceNameResolverChainTests(TestCase):
    """Resolver chains skip empty/sentinel names and stop at the first useful name."""

    @given(name=st.text(min_size=1, max_size=80).filter(is_meaningful_name))
    @hyp_settings(max_examples=25)
    def test_returns_first_meaningful_name(self, name: str) -> None:
        chain = PlaceNameResolverChain(resolvers=(_Resolver(None), _Resolver("No Information Available"), _Resolver(name)))
        self.assertEqual(chain.resolve(40.0, -74.0), name)

    def test_returns_none_when_no_resolver_finds_name(self) -> None:
        chain = PlaceNameResolverChain(resolvers=(_Resolver(None), _Resolver("No Information Available")))
        self.assertIsNone(chain.resolve(40.0, -74.0))

    def test_skips_abandoned_placeholder(self) -> None:
        chain = PlaceNameResolverChain(resolvers=(_Resolver("Abandoned"), _Resolver("Real Mill")))
        self.assertEqual(chain.resolve(40.0, -74.0), "Real Mill")

    def test_skips_coordinate_placeholder(self) -> None:
        chain = PlaceNameResolverChain(resolvers=(_Resolver("40.0, -74.0"), _Resolver("Steel Factory")))
        self.assertEqual(chain.resolve(40.0, -74.0), "Steel Factory")

    def test_google_places_resolver_handles_gateway_errors(self) -> None:
        from urbanlens.dashboard.services.locations.google import GooglePlacesNameResolver

        with (
            mock.patch("urbanlens.dashboard.services.locations.naming.settings.google_unrestricted_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.naming.GooglePlacesGateway") as gateway,
        ):
            gateway.return_value.get_data.side_effect = ValueError("bad response")
            self.assertIsNone(GooglePlacesNameResolver().resolve(40.0, -74.0))


class BoundaryProviderChainTests(TestCase):
    """Default boundary resolution uses providers in order before falling back."""

    def test_chain_returns_first_provider_boundary(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain, default_bbox

        expected = default_bbox(40.0, -74.0)
        provider = mock.Mock(name="provider")
        provider.name = "mock"
        provider.boundary_for_point.return_value = expected

        boundary = BoundaryProviderChain(providers=(provider,)).boundary_for_point(40.0, -74.0, name="Factory")

        self.assertEqual(boundary, expected)
        provider.boundary_for_point.assert_called_once_with(40.0, -74.0, name="Factory")

    def test_chain_falls_back_when_provider_returns_none(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain, default_bbox

        provider = mock.Mock(name="provider")
        provider.name = "mock"
        provider.boundary_for_point.return_value = None

        boundary = BoundaryProviderChain(providers=(provider,)).boundary_for_point(40.0, -74.0)

        self.assertEqual(boundary, default_bbox(40.0, -74.0))

    def test_overpass_provider_selects_smallest_polygon_containing_point(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import OverpassBoundaryProvider

        elements = [
            {
                "type": "way",
                "id": 1,
                "geometry": [
                    {"lat": 39.99, "lon": -74.01},
                    {"lat": 39.99, "lon": -73.99},
                    {"lat": 40.01, "lon": -73.99},
                    {"lat": 40.01, "lon": -74.01},
                    {"lat": 39.99, "lon": -74.01},
                ],
            },
            {
                "type": "way",
                "id": 2,
                "geometry": [
                    {"lat": 39.999, "lon": -74.001},
                    {"lat": 39.999, "lon": -73.999},
                    {"lat": 40.001, "lon": -73.999},
                    {"lat": 40.001, "lon": -74.001},
                    {"lat": 39.999, "lon": -74.001},
                ],
            },
        ]
        gateway = mock.Mock()
        gateway.nearby_boundary_candidates.return_value = elements

        boundary = OverpassBoundaryProvider(gateway=gateway).boundary_for_point(40.0, -74.0)

        self.assertIsNotNone(boundary)
        self.assertAlmostEqual(boundary.area, 0.000004, places=8)


class OverpassGatewayTests(TestCase):
    """Overpass helpers build bounded, reusable API requests."""

    def test_nearby_features_query_can_include_nodes_without_geometry(self) -> None:
        from urbanlens.dashboard.services.apis.locations.overpass import OverpassGateway

        query = OverpassGateway._nearby_features_query(
            40.0,
            -74.0,
            radius_meters=5000,
            tag_filter='["historic"]',
            include_nodes=True,
            include_geometry=False,
        )

        self.assertIn("node(around:250,40.0000000,-74.0000000)", query)
        self.assertIn("way(around:250,40.0000000,-74.0000000)", query)
        self.assertIn("out center tags qt", query)

    def test_element_returns_first_result(self) -> None:
        from urbanlens.dashboard.services.apis.locations.overpass import OverpassGateway

        gateway = OverpassGateway(session=mock.Mock())
        with mock.patch.object(gateway, "elements_for_query", return_value=[{"type": "way", "id": 123}]):
            self.assertEqual(gateway.element("way", 123), {"type": "way", "id": 123})


class NominatimGatewayTests(TestCase):
    """Nominatim search and lookup normalize OSM place payloads."""

    def test_search_normalizes_results(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        response = mock.Mock()
        response.json.return_value = [
            {
                "display_name": "Factory, Example",
                "name": "Factory",
                "osm_type": "way",
                "osm_id": 123,
                "extratags": {"website": "https://example.test"},
            },
        ]
        session = mock.Mock()
        session.get.return_value = response
        gateway = NominatimGateway(session=session)

        results = gateway.search("Factory", limit=100)

        self.assertEqual(results[0]["name"], "Factory")
        self.assertEqual(results[0]["osm_url"], "https://www.openstreetmap.org/way/123")
        self.assertEqual(results[0]["website"], "https://example.test")
        self.assertEqual(session.get.call_args.kwargs["params"]["limit"], 50)

    def test_lookup_limits_batch_size(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        response = mock.Mock()
        response.json.return_value = []
        session = mock.Mock()
        session.get.return_value = response
        gateway = NominatimGateway(session=session)

        gateway.lookup([f"W{i}" for i in range(60)])

        osm_ids = session.get.call_args.kwargs["params"]["osm_ids"]
        self.assertEqual(len(osm_ids.split(",")), 50)
