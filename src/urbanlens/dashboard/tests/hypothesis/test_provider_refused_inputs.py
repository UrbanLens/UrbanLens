"""Each external provider refuses an input it can never answer before the call goes out."""

from __future__ import annotations

import math
from typing import Any
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
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
