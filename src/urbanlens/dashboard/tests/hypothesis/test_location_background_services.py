"""Tests for boundary resolution and place-name resolver services."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest import mock

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations.base import default_bbox
from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain
from urbanlens.dashboard.services.locations.naming import is_meaningful_name


@dataclass(slots=True)
class _Resolver:
    value: str | None

    def resolve(self, latitude: float, longitude: float) -> str | None:
        return self.value


@dataclass(slots=True)
class _StubBoundaryProvider:
    """Minimal typed boundary provider for chain tests."""

    typed: dict
    service_key: str | None = "stub"
    boundary_kind: str = "property"
    calls: list = field(default_factory=list)

    def get_typed_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> dict:
        self.calls.append((latitude, longitude, name))
        return self.typed


class DefaultBoundingBoxTests(SimpleTestCase):
    """Default generated bounding boxes contain the source coordinate."""

    @given(
        latitude=st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False),
        longitude=st.floats(min_value=-179.9, max_value=179.9, allow_nan=False, allow_infinity=False),
    )
    @hyp_settings(max_examples=30)
    def test_default_bbox_surrounds_coordinate(self, latitude: float, longitude: float) -> None:
        bbox = default_bbox(latitude, longitude)
        min_x, min_y, max_x, max_y = bbox.extent
        self.assertLess(min_x, longitude)
        self.assertGreater(max_x, longitude)
        self.assertLess(min_y, latitude)
        self.assertGreater(max_y, latitude)


class PlaceNameResolverChainTests(SimpleTestCase):
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
            mock.patch("urbanlens.dashboard.services.locations.google.settings.google_unrestricted_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.google.GooglePlacesGateway") as gateway,
        ):
            gateway.return_value.get_data.side_effect = ValueError("bad response")
            self.assertIsNone(GooglePlacesNameResolver().resolve(40.0, -74.0))

    def test_google_places_resolver_handles_rate_limit_errors(self) -> None:
        """A rate-limited Google Places call must degrade gracefully, not raise into the caller (TODO: "ugly error page")."""
        from urbanlens.dashboard.services.locations.google import GooglePlacesNameResolver
        from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError

        with (
            mock.patch("urbanlens.dashboard.services.locations.google.settings.google_unrestricted_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.google.GooglePlacesGateway") as gateway,
        ):
            gateway.return_value.get_data.side_effect = RateLimitExceededError("google_places")
            self.assertIsNone(GooglePlacesNameResolver().resolve(40.0, -74.0))

    def test_google_geocoding_resolver_handles_rate_limit_errors(self) -> None:
        from urbanlens.dashboard.services.locations.google import GoogleGeocodingNameResolver
        from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError

        with (
            mock.patch("urbanlens.dashboard.services.locations.google.settings.google_unrestricted_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.google.GoogleGeocodingGateway") as gateway,
        ):
            gateway.return_value.get_place_name.side_effect = RateLimitExceededError("google_geocoding")
            self.assertIsNone(GoogleGeocodingNameResolver().resolve(40.0, -74.0))

    def test_google_places_resolver_skips_locality_only_result_for_next_poi(self) -> None:
        """A bare city hit (e.g. a rural pin with no closer POI) must not become the pin's name.

        Regression test: a golf course with no other nearby Places result used to be
        named "Poughkeepsie" (its enclosing city) because Nearby Search can return a
        locality as its only "establishment" match. The resolver must skip results
        whose types are exclusively administrative/regional ones.
        """
        from urbanlens.dashboard.services.locations.google import GooglePlacesNameResolver

        with (
            mock.patch("urbanlens.dashboard.services.locations.google.settings.google_unrestricted_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.google.GooglePlacesGateway") as gateway,
        ):
            gateway.return_value.get_data.return_value = [
                {"name": "Poughkeepsie", "types": ["locality", "political"]},
                {"name": "College Hill Golf Course", "types": ["golf_course", "point_of_interest", "establishment"]},
            ]
            self.assertEqual(GooglePlacesNameResolver().resolve(40.0, -74.0), "College Hill Golf Course")

    def test_google_places_resolver_returns_none_when_every_result_is_locality_only(self) -> None:
        from urbanlens.dashboard.services.locations.google import GooglePlacesNameResolver

        with (
            mock.patch("urbanlens.dashboard.services.locations.google.settings.google_unrestricted_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.google.GooglePlacesGateway") as gateway,
        ):
            gateway.return_value.get_data.return_value = [{"name": "Poughkeepsie", "types": ["locality", "political"]}]
            self.assertIsNone(GooglePlacesNameResolver().resolve(40.0, -74.0))

    def test_google_geocoding_get_place_name_skips_locality_only_result(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

        gateway = GoogleGeocodingGateway(api_key="key")
        with mock.patch.object(
            GoogleGeocodingGateway,
            "geocode_coordinates",
            return_value={
                "results": [
                    {"formatted_address": "Poughkeepsie, NY 12603, USA", "types": ["locality", "political"]},
                    {"formatted_address": "123 Fairway Dr, Poughkeepsie, NY 12603, USA", "types": ["street_address"]},
                ],
            },
        ):
            self.assertEqual(gateway.get_place_name(40.0, -74.0), "123 Fairway Dr, Poughkeepsie, NY 12603, USA")

    def test_google_geocoding_get_place_name_returns_none_when_only_locality_available(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

        gateway = GoogleGeocodingGateway(api_key="key")
        with mock.patch.object(
            GoogleGeocodingGateway,
            "geocode_coordinates",
            return_value={"results": [{"formatted_address": "Poughkeepsie, NY 12603, USA", "types": ["locality", "political"]}]},
        ):
            self.assertIsNone(gateway.get_place_name(40.0, -74.0))



class BoundaryProviderChainTests(SimpleTestCase):
    """Typed boundary resolution fills property/building slots independently, with no bbox fallback."""

    def test_chain_returns_first_provider_boundary(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

        expected = default_bbox(40.0, -74.0)
        provider = _StubBoundaryProvider(typed={"property": expected})

        resolved = BoundaryProviderChain(providers=(provider,)).get_boundaries(40.0, -74.0, name="Factory")

        self.assertIsNotNone(resolved.property_polygon)
        self.assertEqual(resolved.property_polygon[0].wkt, expected.wkt)
        self.assertIsNone(resolved.building_polygon)
        self.assertEqual(provider.calls, [(40.0, -74.0, "Factory")])

    def test_chain_returns_nothing_when_no_provider_finds_boundary(self) -> None:
        """No static-bbox fallback any more: absence means the circle default applies."""
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

        provider = _StubBoundaryProvider(typed={"property": None, "building": None})

        resolved = BoundaryProviderChain(providers=(provider,)).get_boundaries(40.0, -74.0)

        self.assertIsNone(resolved.property_polygon)
        self.assertIsNone(resolved.building_polygon)
        self.assertIsNone(BoundaryProviderChain(providers=(provider,)).get_boundary(40.0, -74.0))

    def test_later_providers_fill_remaining_slots(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

        building = default_bbox(40.0, -74.0)
        parcel = default_bbox(40.001, -74.001)
        first = _StubBoundaryProvider(typed={"building": building})
        second = _StubBoundaryProvider(typed={"property": parcel, "building": default_bbox(41.0, -75.0)})

        resolved = BoundaryProviderChain(providers=(first, second)).get_boundaries(40.0, -74.0)

        # The building slot keeps the first provider's result; the second
        # provider only fills the still-empty property slot.
        self.assertEqual(resolved.building_polygon[0].wkt, building.wkt)
        self.assertEqual(resolved.property_polygon[0].wkt, parcel.wkt)

    def test_overpass_gateway_selects_smallest_polygon_containing_point(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

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
        gateway = OverpassGateway(session=mock.Mock())

        with mock.patch.object(gateway, "nearby_boundary_candidates", return_value=elements):
            boundary = gateway.get_boundary(40.0, -74.0)

        self.assertIsNotNone(boundary)
        self.assertAlmostEqual(boundary.area, 0.000004, places=8)

    def test_overpass_gateway_classifies_building_tags_separately(self) -> None:
        """Elements tagged ``building`` fill the building slot; everything else is property."""
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        ring = [
            {"lat": 39.99, "lon": -74.01},
            {"lat": 39.99, "lon": -73.99},
            {"lat": 40.01, "lon": -73.99},
            {"lat": 40.01, "lon": -74.01},
            {"lat": 39.99, "lon": -74.01},
        ]
        inner_ring = [
            {"lat": 39.999, "lon": -74.001},
            {"lat": 39.999, "lon": -73.999},
            {"lat": 40.001, "lon": -73.999},
            {"lat": 40.001, "lon": -74.001},
            {"lat": 39.999, "lon": -74.001},
        ]
        elements = [
            {"type": "way", "id": 1, "geometry": ring, "tags": {"landuse": "industrial"}},
            {"type": "way", "id": 2, "geometry": inner_ring, "tags": {"building": "yes"}},
        ]
        gateway = OverpassGateway(session=mock.Mock())

        with mock.patch.object(gateway, "nearby_boundary_candidates", return_value=elements):
            typed = gateway.get_typed_boundaries(40.0, -74.0)

        self.assertIsNotNone(typed["building"])
        self.assertIsNotNone(typed["property"])
        self.assertLess(typed["building"].area, typed["property"].area)


class OverpassGatewayTests(SimpleTestCase):
    """Overpass helpers build bounded, reusable API requests."""

    def test_nearby_features_query_can_include_nodes_without_geometry(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

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

    def test_default_tag_filter_splits_into_valid_clauses(self) -> None:
        """Regression test: Overpass QL has no `|` OR-operator between bracket filters.

        A previous version of `_DEFAULT_FEATURE_TAG_FILTER` chained filters with a bare
        `|` directly inside a single statement, which Overpass rejects with a parse
        error on every request. Clauses must instead be split into separate unioned
        statements.
        """
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import _DEFAULT_FEATURE_TAG_FILTER, OverpassGateway

        query = OverpassGateway._nearby_features_query(
            40.0,
            -74.0,
            radius_meters=100,
            tag_filter=_DEFAULT_FEATURE_TAG_FILTER,
            include_nodes=True,
            include_geometry=True,
        )

        self.assertNotIn("]|[", query)
        self.assertIn('["railway"="station"]', query)
        self.assertIn('~"^(building|amenity', query)

    def test_element_returns_first_result(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        gateway = OverpassGateway(session=mock.Mock())
        with mock.patch.object(gateway, "elements_for_query", return_value=[{"type": "way", "id": 123}]):
            self.assertEqual(gateway.element("way", 123), {"type": "way", "id": 123})

    def test_query_failure_degrades_to_empty_list_without_a_traceback(self) -> None:
        """The public overpass-api.de instance routinely times out/429s/504s under
        normal load (shared community infrastructure, per its own ServiceDefaults
        note) - this must never propagate, and shouldn't log at a level that
        makes routine external flakiness look like an UrbanLens crash."""
        import requests

        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        gateway = OverpassGateway(session=mock.Mock())
        with (
            mock.patch.object(gateway, "query", side_effect=requests.exceptions.ReadTimeout("timed out")),
            self.assertLogs("urbanlens.dashboard.services.apis.locations.boundaries.overpass", level="WARNING") as logs,
        ):
            result = gateway.elements_for_query("[out:json];node(1);out;")

        self.assertEqual(result, [])
        self.assertTrue(any("overpass query failed" in message.lower() for message in logs.output))

    def test_non_json_response_also_degrades_gracefully(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        gateway = OverpassGateway(session=mock.Mock())
        with mock.patch.object(gateway, "query", side_effect=ValueError("not json")):
            result = gateway.elements_for_query("[out:json];node(1);out;")

        self.assertEqual(result, [])


class NominatimGatewayTests(SimpleTestCase):
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

    def test_reverse_geocode_surfaces_address_breakdown_as_extra_details(self) -> None:
        """A point with no OSM tags of its own still gets neighbourhood/postcode/
        county from addressdetails=1, so the pin-detail panel isn't empty for
        the common "no extra tags" case."""
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        response = mock.Mock()
        response.json.return_value = {
            "display_name": "1265 Section Rd, Cincinnati, OH 45237, USA",
            "address": {"neighbourhood": "College Hill", "county": "Hamilton County", "postcode": "45237"},
        }
        session = mock.Mock()
        session.get.return_value = response
        gateway = NominatimGateway(session=session)

        result = gateway.reverse_geocode(39.19749, -84.46964)

        assert result is not None
        labels = [detail["label"] for detail in result["extra_details"]]
        self.assertIn("Neighbourhood", labels)
        self.assertIn("County", labels)
        self.assertIn("Postcode", labels)

    def test_suburb_is_dropped_when_identical_to_neighbourhood(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        response = mock.Mock()
        response.json.return_value = {"address": {"neighbourhood": "College Hill", "suburb": "College Hill"}}
        session = mock.Mock()
        session.get.return_value = response
        gateway = NominatimGateway(session=session)

        result = gateway.reverse_geocode(39.19749, -84.46964)

        assert result is not None
        labels = [detail["label"] for detail in result["extra_details"]]
        self.assertEqual(labels.count("Neighbourhood") + labels.count("Suburb"), 1)

    def test_missing_address_breakdown_is_not_an_error(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        response = mock.Mock()
        response.json.return_value = {"display_name": "Nowhere"}
        session = mock.Mock()
        session.get.return_value = response
        gateway = NominatimGateway(session=session)

        result = gateway.reverse_geocode(0, 0)

        assert result is not None
        self.assertEqual(result["extra_details"], [])


class NominatimInfoViewTests(TestCase):
    """PinController.nominatim_info's "is this worth rendering" gate."""

    def setUp(self) -> None:
        from django.contrib.auth.models import User
        from model_bakery import baker

        from urbanlens.dashboard.models.profile.model import Profile

        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        user = baker.make(User)
        self.profile = Profile.objects.get(user=user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.client.force_login(user)

    def _cache_and_fetch(self, data: dict):
        from django.urls import reverse

        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(self.pin.location, "nominatim", data, query_key="")
        return self.client.get(reverse("pin.nominatim", args=[self.pin.slug]))

    def test_email_only_result_is_rendered_not_204(self) -> None:
        """email was previously missing from the gate's useful-fields tuple even
        though the template already renders it - a place with only an email
        would 204 instead of showing that one fact."""
        response = self._cache_and_fetch({"email": "info@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "info@example.com")

    def test_address_breakdown_only_result_is_rendered_not_204(self) -> None:
        """A point with no OSM tags of its own, only the neighbourhood/postcode/
        county now folded into extra_details, still renders."""
        response = self._cache_and_fetch({"extra_details": [{"key": "postcode", "label": "Postcode", "value": "45237"}]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "45237")

    def test_no_useful_fields_returns_204(self) -> None:
        response = self._cache_and_fetch({"lat": "39.19749", "lon": "-84.46964"})
        self.assertEqual(response.status_code, 204)
