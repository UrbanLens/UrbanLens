"""A Google geocode is asked for once per address or point, not on every lookup.

``GeocodedLocation`` keyed a stored answer by ``request_data["place_name"]``, which no request carries, and a reverse
geocode by the answer's own coordinates rather than the point asked about. An address was never read back, and a point
only when it stood exactly where an earlier answer did: dev held 2,224 rows with no key, 576 answer locations more than
once.
"""

from __future__ import annotations

from unittest import mock

import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

_ANSWER = {
    "status": "OK",
    "results": [
        {
            "formatted_address": "75 Market St, Poughkeepsie, NY 12601, USA",
            "types": ["street_address"],
            "geometry": {"location": {"lat": 41.7003521, "lng": -73.9211874}},
        },
    ],
}


def _response(body: dict) -> mock.Mock:
    response = mock.Mock(spec=requests.Response, status_code=200)
    response.json.return_value = body
    return response


class GeocodeCacheTests(TestCase):
    def _gateway(self, *bodies: dict) -> GoogleGeocodingGateway:
        session = mock.Mock()
        session.get.side_effect = [_response(body) for body in bodies]
        return GoogleGeocodingGateway(api_key="test-key", session=session)

    def test_a_point_is_reverse_geocoded_once(self) -> None:
        gateway = self._gateway(_ANSWER, _ANSWER)

        first = gateway.geocode_coordinates(41.70041, -73.92103)
        second = gateway.geocode_coordinates(41.70041, -73.92103)

        self.assertEqual(first, _ANSWER)
        self.assertEqual(second, _ANSWER)
        self.assertEqual(gateway.session.get.call_count, 1)

    def test_a_nearby_point_is_not_answered_from_another_points_row(self) -> None:
        gateway = self._gateway(_ANSWER, _ANSWER)

        gateway.geocode_coordinates(41.70041, -73.92103)
        gateway.geocode_coordinates(41.7003521, -73.9211874)

        self.assertEqual(gateway.session.get.call_count, 2)

    def test_an_address_is_geocoded_once(self) -> None:
        gateway = self._gateway(_ANSWER, _ANSWER)

        gateway.geocode_place_name("75 Market St, Poughkeepsie")
        self.assertEqual(gateway.geocode_place_name("75 Market St, Poughkeepsie"), _ANSWER)

        self.assertEqual(gateway.session.get.call_count, 1)

    def test_a_refusal_is_not_kept(self) -> None:
        refused = {
            "status": "OVER_QUERY_LIMIT",
            "results": [],
            "error_message": "You have exceeded your daily request quota",
        }
        gateway = self._gateway(refused, _ANSWER)

        gateway.geocode_coordinates(41.70041, -73.92103)

        self.assertEqual(gateway.geocode_coordinates(41.70041, -73.92103), _ANSWER)
        self.assertEqual(gateway.session.get.call_count, 2)


class PlaceNameLookupTests(TestCase):
    def test_a_failed_name_lookup_asks_google_once(self) -> None:
        """The resolver chain already ends with Google Geocoding; the service asked it again when the chain found nothing."""
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService
        from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain

        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.places_resolution.resolve_name_from_nearby",
                return_value=None,
            ),
            mock.patch.object(
                GoogleGeocodingGateway, "get_place_name", side_effect=ConnectionError("unreachable")
            ) as lookup,
        ):
            name = GooglePlaceService(name_resolver=PlaceNameResolverChain())._resolve_name(41.70041, -73.92103)

        self.assertIsNone(name)
        self.assertEqual(lookup.call_count, 1)
