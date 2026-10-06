"""Each external provider refuses an input it can never answer before the call goes out."""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError, InputRejection


class GooglePlacesTests(TestCase):
    def _gateway(self) -> tuple[GooglePlacesGateway, mock.Mock]:
        session = mock.Mock()
        return GooglePlacesGateway(api_key="test-key", session=session), session

    def test_nearby_search_refuses_points_and_radii_google_cannot_search(self) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {"latitude": 0.0, "longitude": 0.0},
            {"latitude": math.nan, "longitude": -73.9},
            {"latitude": 41.7, "longitude": -73.9, "radius": 0},
            {"latitude": 41.7, "longitude": -73.9, "radius": 50_001},
        )
        for kwargs in cases:
            gateway, session = self._gateway()
            with self.subTest(kwargs=kwargs), self.assertRaises(ImpossibleInputError):
                gateway.search_nearby(**kwargs)
            session.post.assert_not_called()

    def test_legacy_nearby_search_refuses_null_island(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError):
            gateway.get_data(0, 0)
        session.get.assert_not_called()

    def test_ids_that_cannot_be_place_ids_never_reach_google(self) -> None:
        gateway, session = self._gateway()
        for place_id in ("", "places/ChIJabc", "https://maps.google.com/?cid=1"):
            with self.subTest(place_id=place_id):
                with self.assertRaises(ImpossibleInputError):
                    gateway.get_place_details(place_id, ["name"])
                with self.assertRaises(ImpossibleInputError):
                    gateway.get_place_photo_names(place_id)
        session.get.assert_not_called()

    def test_photo_names_and_widths_google_would_refuse_never_reach_it(self) -> None:
        gateway, session = self._gateway()
        for photo_name, max_width in (
            ("ChIJabc", 1200),
            ("places/ChIJabc/photos/", 1200),
            ("places/ChIJabc/photos/Ael Y", 1200),
            ("places/ChIJabc/photos/AelY", 0),
            ("places/ChIJabc/photos/AelY", 4801),
        ):
            with self.subTest(photo_name=photo_name, max_width=max_width), self.assertRaises(ImpossibleInputError):
                gateway.get_photo_media(photo_name, max_width=max_width)
        session.get.assert_not_called()

    def test_blank_autocomplete_never_reaches_google(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError) as caught:
            gateway.autocomplete("   ")
        self.assertIs(caught.exception.reason, InputRejection.EMPTY_QUERY)
        session.get.assert_not_called()

    def test_a_real_search_still_goes_out(self) -> None:
        gateway, session = self._gateway()
        session.post.return_value = mock.Mock(**{"json.return_value": {"places": []}})
        gateway.search_nearby(41.7, -73.9, radius=500)
        session.post.assert_called_once()


class GoogleGeocodingTests(TestCase):
    def _gateway(self) -> tuple[GoogleGeocodingGateway, mock.Mock]:
        session = mock.Mock()
        return GoogleGeocodingGateway(api_key="test-key", session=session), session

    def test_reverse_geocoding_refuses_points_that_cannot_have_an_address(self) -> None:
        for latitude, longitude in ((0.0, 0.0), (math.inf, 1.0), (95.0, 1.0)):
            gateway, session = self._gateway()
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(ImpossibleInputError):
                gateway.geocode_coordinates(latitude, longitude)
            session.get.assert_not_called()

    def test_a_whitespace_address_never_reaches_google(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError):
            gateway.geocode_place_name("   ")
        session.get.assert_not_called()

    def test_the_name_and_coordinate_helpers_answer_none_for_a_refused_input(self) -> None:
        gateway, session = self._gateway()
        self.assertIsNone(gateway.get_place_name(0, 0))
        self.assertEqual(gateway.get_coordinates("  "), (None, None))
        session.get.assert_not_called()

    def test_an_address_backfill_for_null_island_writes_nothing_and_raises_nothing(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.locations.addresses import ensure_location_address
        from urbanlens.UrbanLens.settings.app import settings

        location = baker.make(Location, latitude=0, longitude=0, route="", google_place=None)
        with (
            mock.patch.object(settings, "google_unrestricted_api_key", "test-key"),
            mock.patch("urbanlens.dashboard.services.core.rate_limiter._RateLimitedSession._do_request") as request,
        ):
            self.assertFalse(ensure_location_address(location))
        request.assert_not_called()


class NominatimTests(TestCase):
    def _gateway(self) -> tuple[Any, mock.Mock]:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        session = mock.Mock()
        session.headers = {}
        return NominatimGateway(session=session), session

    def test_a_blank_search_never_reaches_nominatim(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError):
            gateway.search("   ")
        session.get.assert_not_called()

    def test_a_lookup_of_only_malformed_ids_never_reaches_nominatim(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError) as caught:
            gateway.lookup(["123", "X5", "W-1"])
        self.assertIs(caught.exception.reason, InputRejection.MALFORMED_ID)
        session.get.assert_not_called()

    def test_malformed_ids_are_dropped_from_a_lookup_that_has_real_ones(self) -> None:
        gateway, session = self._gateway()
        session.get.return_value = mock.Mock(**{"json.return_value": []})
        gateway.lookup(["N123", "bogus", "w456"])
        self.assertEqual(session.get.call_args.kwargs["params"]["osm_ids"], "N123,w456")

    def test_reverse_geocoding_null_island_never_reaches_nominatim(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError):
            gateway.reverse_geocode(0.0, 0.0)
        with self.assertRaises(ImpossibleInputError):
            gateway.reverse_geocode_admin(math.nan, 1.0)
        session.get.assert_not_called()

    def test_forward_geocoding_a_blank_address_answers_nothing(self) -> None:
        from urbanlens.dashboard.services.apis.locations.geocode_resolution import nominatim_geocode

        with mock.patch("urbanlens.dashboard.services.core.rate_limiter._RateLimitedSession._do_request") as request:
            self.assertEqual(nominatim_geocode("  "), (None, None))
        request.assert_not_called()


class StreetViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_a_coverage_probe_of_null_island_never_reaches_google(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.street_view_metadata import (
            GoogleStreetViewMetadataGateway,
        )

        session = mock.Mock()
        with self.assertRaises(ImpossibleInputError):
            GoogleStreetViewMetadataGateway(api_key="test-key", session=session).has_imagery(0.0, 0.0)
        session.get.assert_not_called()

    def test_the_right_click_probe_answers_no_imagery_for_a_refused_point(self) -> None:
        baker.make(User)  # the first user is auto-promoted to site admin
        self.client.force_login(baker.make(User))
        with (
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "test-key"),
            mock.patch("requests.Session.request") as wire,
        ):
            response = self.client.get(reverse("map.streetview_check"), {"lat": "0", "lng": "0"})
        self.assertEqual(response.json(), {"available": False})
        wire.assert_not_called()

    def test_static_street_view_of_null_island_never_reaches_google(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        session = mock.Mock()
        with self.assertRaises(ImpossibleInputError):
            GoogleMapsGateway(api_key="test-key", session=session).get_street_view_single(0.0, 0.0)
        session.get.assert_not_called()

    def test_a_refused_point_is_a_settled_empty_carousel_not_a_degraded_one(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        session = mock.Mock()
        fetched = GoogleMapsGateway(api_key="test-key", session=session).get_street_view_slides(0.0, 0.0)
        self.assertEqual(fetched.slides, [])
        self.assertFalse(fetched.degraded)
        session.get.assert_not_called()


class GoogleStaticSatelliteTests(TestCase):
    def test_an_impossible_centre_never_reaches_google(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        for latitude, longitude in ((math.nan, 1.0), (91.0, 1.0)):
            session = mock.Mock()
            with self.subTest(latitude=latitude), self.assertRaises(ImpossibleInputError):
                GoogleMapsGateway(api_key="test-key", session=session).get_satellite_image_bytes(latitude, longitude)
            session.get.assert_not_called()

    def test_null_island_has_imagery_so_it_is_still_asked(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        session = mock.Mock()
        session.get.return_value = mock.Mock(content=b"jpeg")
        self.assertEqual(
            GoogleMapsGateway(api_key="test-key", session=session).get_satellite_image_bytes(0.0, 0.0), b"jpeg"
        )


class VirusTotalTests(TestCase):
    def test_a_value_that_is_not_a_sha256_never_reaches_virustotal(self) -> None:
        from urbanlens.dashboard.services.apis.security.virustotal import VirusTotalGateway

        session = mock.Mock()
        gateway = VirusTotalGateway(api_key="k", session=session)
        for value in ("", "a" * 63, "g" * 64, "../" + "a" * 61):
            with self.subTest(value=value), self.assertRaises(ImpossibleInputError):
                gateway.get_file_report(value)
        session.get.assert_not_called()

    def test_a_refused_hash_is_no_verdict_so_clamav_decides(self) -> None:
        from urbanlens.dashboard.services.security.virustotal_scan import VirusTotalNoVerdictError, verdict_for_checksum

        with (
            mock.patch("urbanlens.UrbanLens.settings.app.settings.virustotal_api_key", "k"),
            mock.patch("requests.Session.request") as wire,
            self.assertRaises(VirusTotalNoVerdictError),
        ):
            verdict_for_checksum("not-a-hash")
        wire.assert_not_called()


class TwilioTests(TestCase):
    def _sms(self) -> tuple[Any, mock.Mock]:
        from urbanlens.dashboard.services.apis.messaging.sms import SmsGateway

        session = mock.Mock()
        return SmsGateway(account_sid="AC1", auth_token="t", from_number="+15550000000", session=session), session

    def test_a_number_that_cannot_be_dialled_is_not_sent(self) -> None:
        gateway, session = self._sms()
        for number in ("", "call me", "+1", "12-ab"):
            with self.subTest(number=number):
                self.assertFalse(gateway.send(number, "hello"))
        session.post.assert_not_called()
        self.assertEqual(ApiCallLog.objects.filter(service="sms", was_rejected_input=True).count(), 4)

    def test_an_empty_message_is_not_sent(self) -> None:
        gateway, session = self._sms()
        self.assertFalse(gateway.send("+15551234567", ""))
        session.post.assert_not_called()

    def test_a_formatted_or_whatsapp_number_is_still_sent(self) -> None:
        from urbanlens.dashboard.services.apis.messaging.whatsapp import WhatsAppGateway

        gateway, session = self._sms()
        self.assertTrue(gateway.send("+1 (555) 123-4567", "hello"))
        session.post.assert_called_once()

        session = mock.Mock()
        whatsapp = WhatsAppGateway(account_sid="AC1", auth_token="t", from_number="+15550000000", session=session)
        self.assertTrue(whatsapp.send("whatsapp:+15551234567", "hello"))
        session.post.assert_called_once()


class AzureMapsTests(TestCase):
    def _search(self) -> tuple[Any, mock.Mock]:
        from urbanlens.dashboard.services.apis.locations.azure.search import AzureMapsSearchGateway

        session = mock.Mock()
        return AzureMapsSearchGateway(subscription_key="k", session=session), session

    def _geocoding(self) -> tuple[Any, mock.Mock]:
        from urbanlens.dashboard.services.apis.locations.azure.geocoding import AzureMapsGeocodingGateway

        session = mock.Mock()
        return AzureMapsGeocodingGateway(subscription_key="k", session=session), session

    def test_search_refuses_a_blank_query_or_an_impossible_bias_point(self) -> None:
        gateway, session = self._search()
        self.assertEqual(gateway.search(""), [])
        with self.assertRaises(ImpossibleInputError):
            gateway.search("   ")
        with self.assertRaises(ImpossibleInputError):
            gateway.search("asylum", latitude=math.nan, longitude=1.0)
        session.get.assert_not_called()

    def test_poi_search_refuses_null_island_and_an_empty_circle(self) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {"latitude": 0.0, "longitude": 0.0},
            {"latitude": 41.7, "longitude": -73.9, "radius": 0},
            {"latitude": 41.7, "longitude": -73.9, "radius": 50_001},
        )
        for kwargs in cases:
            gateway, session = self._search()
            with self.subTest(kwargs=kwargs), self.assertRaises(ImpossibleInputError):
                gateway.search_poi(**kwargs)
            session.get.assert_not_called()

    def test_geocoding_refuses_a_blank_address_and_an_impossible_point(self) -> None:
        gateway, session = self._geocoding()
        self.assertIsNone(gateway.geocode_address(""))
        with self.assertRaises(ImpossibleInputError):
            gateway.geocode_address("  ")
        for latitude, longitude in ((0.0, 0.0), (math.inf, 1.0)):
            with self.subTest(latitude=latitude), self.assertRaises(ImpossibleInputError):
                gateway.reverse_geocode(latitude, longitude)
        session.get.assert_not_called()


class WikipediaTests(TestCase):
    def _gateway(self) -> tuple[Any, mock.Mock]:
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        session = mock.Mock()
        return WikipediaGateway(session=session), session

    def test_an_impossible_point_or_an_empty_circle_never_reaches_wikipedia(self) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {"latitude": math.nan, "longitude": 1.0},
            {"latitude": 41.7, "longitude": -73.9, "radius_m": 0},
            {"latitude": 41.7, "longitude": -73.9, "limit": 0},
        )
        for kwargs in cases:
            gateway, session = self._gateway()
            with self.subTest(kwargs=kwargs), self.assertRaises(ImpossibleInputError):
                gateway.get_nearby_articles(**kwargs)
            session.get.assert_not_called()

    def test_null_island_has_an_article_so_it_is_still_asked(self) -> None:
        gateway, session = self._gateway()
        session.get.return_value = mock.Mock(**{"json.return_value": {"query": {"geosearch": []}}})
        gateway.get_nearby_articles(0.0, 0.0)
        session.get.assert_called_once()


class OverpassTests(TestCase):
    def _gateway(self) -> tuple[Any, mock.Mock]:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        session = mock.Mock()
        return OverpassGateway(session=session), session

    def test_null_island_or_a_non_finite_point_never_reaches_overpass(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError):
            gateway.nearby_features(0.0, 0.0)
        with self.assertRaises(ImpossibleInputError):
            gateway.nearby_boundary_candidates(math.nan, 1.0)
        session.post.assert_not_called()
        session.get.assert_not_called()

    def test_an_id_osm_never_issues_never_reaches_overpass(self) -> None:
        gateway, session = self._gateway()
        with self.assertRaises(ImpossibleInputError):
            gateway.element("node", 0)
        session.post.assert_not_called()
        session.get.assert_not_called()

    def test_the_boundary_chain_moves_past_a_refusal_without_deferring_or_a_traceback(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

        gateway, _session = self._gateway()
        with self.assertNoLogs("urbanlens.dashboard.services.locations.boundaries", level="WARNING"):
            resolved = BoundaryProviderChain(providers=(gateway,)).get_boundaries(0.0, 0.0)
        self.assertEqual(resolved.deferred, [])
        self.assertIsNone(resolved.property_polygon)


class OpenHistoricalMapTests(TestCase):
    def test_null_island_never_reaches_ohm(self) -> None:
        from urbanlens.dashboard.services.apis.locations.open_historical_map import OpenHistoricalMapGateway

        session = mock.Mock()
        gateway = OpenHistoricalMapGateway(session=session)
        with self.assertRaises(ImpossibleInputError):
            gateway.get_coverage(0.0, 0.0)
        with self.assertRaises(ImpossibleInputError):
            gateway.get_features_at(math.nan, 1.0, 1900)
        with self.assertRaises(ValueError):
            gateway.get_features_at(41.7, -73.9, 1)
        session.post.assert_not_called()
        session.get.assert_not_called()

    def test_features_for_a_refused_location_are_an_empty_collection(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.locations import temporal_imagery

        location = baker.make(Location, latitude=0, longitude=0)
        with (
            mock.patch.object(temporal_imagery, "redata_configured", return_value=False),
            mock.patch("requests.Session.request") as wire,
        ):
            self.assertEqual(
                temporal_imagery.get_temporal_features(location, 1900), {"type": "FeatureCollection", "features": []}
            )
        wire.assert_not_called()


class WeatherTests(TestCase):
    def test_open_meteo_answers_none_for_an_impossible_point_without_asking(self) -> None:
        from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

        session = mock.Mock()
        gateway = OpenMeteoGateway(session=session)
        self.assertIsNone(gateway.get_weather_forecast(math.nan, 1.0))
        self.assertIsNone(gateway.get_sun_times(91.0, 0.0))
        session.get.assert_not_called()
        self.assertEqual(ApiCallLog.objects.filter(service="open_meteo", was_rejected_input=True).count(), 2)

    def test_open_meteo_still_forecasts_null_island(self) -> None:
        from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

        session = mock.Mock()
        session.get.return_value = mock.Mock(**{"json.return_value": {}})
        OpenMeteoGateway(session=session).get_weather_forecast(0.0, 0.0)
        session.get.assert_called_once()

    def test_openweathermap_answers_none_for_an_impossible_point_without_asking(self) -> None:
        from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

        session = mock.Mock()
        gateway = OpenWeatherMapGateway(api_key="k", session=session)
        self.assertIsNone(gateway.get_weather_forecast(math.nan, 1.0))
        self.assertIsNone(gateway.get_raw_forecast(Decimal("NaN"), Decimal(1)))
        session.get.assert_not_called()


class RoutingTests(TestCase):
    def test_osrm_answers_no_route_for_null_island_without_asking(self) -> None:
        from urbanlens.dashboard.services.apis.routing.osrm import OSRMGateway

        session = mock.Mock()
        gateway = OSRMGateway(session=session)
        self.assertIsNone(gateway.get_route([(0.0, 0.0), (41.7, -73.9)]))
        self.assertIsNone(gateway.get_route([(41.7, -73.9), (math.nan, 1.0)]))
        session.get.assert_not_called()

    def test_a_refused_waypoint_asks_neither_redata_nor_osrm(self) -> None:
        from urbanlens.dashboard.services.apis.locations import routing_resolution

        with (
            mock.patch.object(routing_resolution, "redata_configured", return_value=True),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.redata_api_url", "https://redata.example.test"),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.redata_api_key", "test-key"),
            mock.patch("requests.Session.request") as wire,
        ):
            self.assertIsNone(routing_resolution.get_route_between((0.0, 0.0), (41.7, -73.9)))
        wire.assert_not_called()


GOOGLE_ONLY = mock.Mock(google_unrestricted_api_key="k", redata_api_url=None, redata_api_key=None)


class NullIslandCallerTests(TestCase):
    """Callers of a gateway that refuses (0, 0) answer "nothing here" rather than raise."""

    def test_the_google_name_resolver_finds_no_name_at_null_island(self) -> None:
        from urbanlens.dashboard.services.apis.locations import places_resolution

        with (
            mock.patch.object(places_resolution, "settings", GOOGLE_ONLY),
            mock.patch("requests.Session.request") as wire,
        ):
            self.assertIsNone(places_resolution.resolve_name_from_nearby(0.0, 0.0, 50.0, api_key="k"))
        wire.assert_not_called()

    def test_the_country_backfill_skips_a_null_island_row_and_carries_on(self) -> None:
        from django.core.management import call_command

        from urbanlens.dashboard.models.location.model import Location

        baker.make(Location, latitude="0.000000", longitude="0.000000", country="")
        real = baker.make(Location, latitude="41.700000", longitude="-73.900000", country="")
        answer = {
            "results": [
                {
                    "address_components": [
                        {"long_name": "United States", "short_name": "US", "types": ["country", "political"]}
                    ]
                }
            ]
        }
        original = GoogleGeocodingGateway.geocode_coordinates

        def geocode(gateway: GoogleGeocodingGateway, latitude: float, longitude: float) -> Any:
            # (0, 0) goes through the real method, which refuses it before any I/O.
            return original(gateway, latitude, longitude) if (latitude, longitude) == (0.0, 0.0) else answer

        with (
            mock.patch("urbanlens.dashboard.management.commands.backfill_location_country.app_settings", GOOGLE_ONLY),
            mock.patch.object(GoogleGeocodingGateway, "geocode_coordinates", autospec=True, side_effect=geocode),
            mock.patch("requests.Session.request") as wire,
        ):
            call_command("backfill_location_country", "--sleep", "0", stdout=mock.Mock(), stderr=mock.Mock())
        wire.assert_not_called()
        real.refresh_from_db()
        self.assertTrue(real.country)

    def test_place_photos_retire_a_null_island_location_with_a_marker(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.photos.photo_enrichment import PlacePhotoEnrichmentSource

        location = baker.make(Location, latitude="0.000000", longitude="0.000000")
        source = PlacePhotoEnrichmentSource()
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.settings", GOOGLE_ONLY),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "k"),
            mock.patch("requests.Session.request") as wire,
        ):
            self.assertTrue(source.enrich(location))
        wire.assert_not_called()
        self.assertTrue(LocationCache.objects.filter(location=location, source=source.marker_source).exists())
        self.assertFalse(Location.objects.filter(source.missing_filter(), pk=location.pk).exists())
