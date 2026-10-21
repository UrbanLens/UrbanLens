"""With REData configured, name, address and DM-address geocoding go to it and not to Google with this deployment's key (UL-344)."""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.apis.locations.geocode_resolution import ReverseGeocode
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.locations.addresses import ensure_location_address
from urbanlens.dashboard.services.locations.google import GoogleGeocodingNameResolver
from urbanlens.dashboard.services.messaging import dm_location_detection
from urbanlens.plugins.redata.gateways.context import LocationContextEnvelope, LocationContextUnavailableError

_CONFIGURED = "urbanlens.plugins.redata.providers.geocode.redata_configured"
_REDATA = "urbanlens.plugins.redata.providers.geocode.RedataGeocodeGateway"
_GOOGLE = "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway"
_GOOGLE_NAME = "urbanlens.dashboard.services.locations.google.GoogleGeocodingGateway"
_PERMITTED = "urbanlens.dashboard.services.core.egress.direct_fallback_permitted"
_KEY = "urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key"
_NOMINATIM = "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway"

_HOUSE = {
    "name": "",
    "display_name": "12, Mill Street, Beacon, New York, United States",
    "place_type": "house",
    "house_number": "12",
    "street": "Mill Street",
    "locality": "Beacon",
    "region": "New York",
    "postal_code": "12508",
    "country": "United States",
    "latitude": 41.5,
    "longitude": -73.9,
    "attributes": {"address": {"county": "Dutchess County"}},
}


def _envelope(*results: dict) -> LocationContextEnvelope:
    return LocationContextEnvelope(count=len(results), complete=True, results=list(results))


def _redata(*results: dict, error: Exception | None = None) -> mock.MagicMock:
    gateway = mock.MagicMock()
    for method in (gateway.return_value.reverse_geocode, gateway.return_value.geocode):
        method.side_effect = error
        method.return_value = _envelope(*results)
    return gateway


class NameResolverTests(TestCase):
    def test_configured_it_names_the_place_from_redata_without_asking_google(self) -> None:
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(_HOUSE)),
            mock.patch(_GOOGLE_NAME) as google,
        ):
            resolver = GoogleGeocodingNameResolver()
            self.assertFalse(resolver.direct)
            self.assertEqual(resolver.resolve(41.5, -73.9), _HOUSE["display_name"])
        google.assert_not_called()

    def test_a_town_is_not_a_name_and_nothing_goes_to_google(self) -> None:
        town = {**_HOUSE, "place_type": "city", "street": ""}
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(town)),
            mock.patch(_GOOGLE_NAME) as google,
        ):
            self.assertIsNone(GoogleGeocodingNameResolver().resolve(41.5, -73.9))
        google.assert_not_called()

    def test_off_production_an_outage_is_no_name_and_not_google(self) -> None:
        down = LocationContextUnavailableError("source_error", "down")
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(error=down)),
            mock.patch(_PERMITTED, return_value=False),
            mock.patch(_GOOGLE_NAME) as google,
        ):
            self.assertIsNone(GoogleGeocodingNameResolver().resolve(41.5, -73.9))
        google.assert_not_called()

    def test_on_production_an_outage_falls_back_to_google(self) -> None:
        down = LocationContextUnavailableError("source_error", "down")
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(error=down)),
            mock.patch(_PERMITTED, return_value=True),
            mock.patch(_KEY, "key"),
            mock.patch(_GOOGLE_NAME) as google,
        ):
            google.return_value.get_place_name.return_value = "Google name"
            self.assertEqual(GoogleGeocodingNameResolver().resolve(41.5, -73.9), "Google name")

    def test_unconfigured_it_asks_google_as_before(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False), mock.patch(_KEY, "key"), mock.patch(_GOOGLE_NAME) as google:
            google.return_value.get_place_name.return_value = "Google name"
            resolver = GoogleGeocodingNameResolver()
            self.assertTrue(resolver.direct)
            self.assertEqual(resolver.resolve(41.5, -73.9), "Google name")


class AddressBackfillTests(TestCase):
    def _location(self) -> Location:
        return baker.make(
            Location,
            latitude=41.5,
            longitude=-73.9,
            route=None,
            street_number=None,
            locality=None,
            administrative_area_level_1=None,
            administrative_area_level_2=None,
            zipcode=None,
            country="",
        )

    def test_configured_it_fills_the_address_from_redata_without_asking_google(self) -> None:
        location = self._location()
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(_HOUSE)),
            mock.patch(_GOOGLE) as google,
        ):
            self.assertTrue(ensure_location_address(location))
        google.assert_not_called()
        location.refresh_from_db()
        self.assertEqual((location.street_number, location.route, location.locality), ("12", "Mill Street", "Beacon"))
        self.assertEqual((location.administrative_area_level_2, location.zipcode), ("Dutchess County", "12508"))

    def test_a_reverse_geocode_without_a_county_leaves_openstreetmap_to_fill_it(self) -> None:
        location = self._location()
        no_county = {**_HOUSE, "attributes": {}}
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(no_county)),
            mock.patch(_GOOGLE) as google,
            mock.patch(_NOMINATIM) as nominatim,
        ):
            nominatim.return_value.reverse_geocode_admin.return_value = {
                "county": "Dutchess County",
                "city": "City of Beacon",
            }
            self.assertTrue(ensure_location_address(location))
        google.assert_not_called()
        location.refresh_from_db()
        self.assertEqual(
            (location.route, location.locality, location.administrative_area_level_2),
            ("Mill Street", "Beacon", "Dutchess County"),
        )

    def test_unconfigured_it_asks_google_as_before(self) -> None:
        location = self._location()
        body = {
            "results": [
                {"address_components": [{"long_name": "Mill Street", "short_name": "Mill St", "types": ["route"]}]}
            ]
        }
        with (
            mock.patch(_CONFIGURED, return_value=False),
            mock.patch(_KEY, "key"),
            mock.patch(_GOOGLE) as google,
            mock.patch(_NOMINATIM) as nominatim,
        ):
            google.return_value.geocode_coordinates.return_value = body
            nominatim.return_value.reverse_geocode_admin.return_value = None
            self.assertTrue(ensure_location_address(location))
        google.return_value.geocode_coordinates.assert_called_once()


class DmAddressTests(TestCase):
    def test_configured_a_street_match_comes_from_redata_without_asking_google(self) -> None:
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(_HOUSE)),
            mock.patch(_GOOGLE) as google,
        ):
            self.assertEqual(dm_location_detection._geocode_address("12 Mill Street"), (41.5, -73.9))
        google.assert_not_called()

    def test_configured_a_match_without_a_street_is_no_match(self) -> None:
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata({**_HOUSE, "street": ""})),
            mock.patch(_GOOGLE) as google,
        ):
            self.assertIsNone(dm_location_detection._geocode_address("Beacon"))
        google.assert_not_called()

    def test_an_outage_off_production_reaches_the_task_so_it_retries(self) -> None:
        down = LocationContextUnavailableError("source_error", "down")
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(_REDATA, _redata(error=down)),
            mock.patch(_PERMITTED, return_value=False),
            mock.patch(_GOOGLE) as google,
            self.assertRaises(OSError),
        ):
            dm_location_detection._geocode_address("12 Mill Street")
        google.assert_not_called()

    def test_unconfigured_it_asks_google_as_before(self) -> None:
        result = {"results": [{"types": ["street_address"], "geometry": {"location": {"lat": 42.5, "lng": -73.5}}}]}
        with mock.patch(_CONFIGURED, return_value=False), mock.patch(_KEY, "key"), mock.patch(_GOOGLE) as google:
            google.return_value.geocode_place_name.return_value = result
            self.assertEqual(dm_location_detection._geocode_address("12 Mill Street"), (42.5, -73.5))


class ReverseResultTests(TestCase):
    def test_the_county_comes_from_either_osm_geocoder_shape(self) -> None:
        from urbanlens.plugins.redata.providers.geocode import _reverse_result

        self.assertEqual(_reverse_result({"attributes": {"county": "Ulster"}}).county, "Ulster")
        self.assertEqual(_reverse_result(_HOUSE).county, "Dutchess County")
        self.assertIsInstance(_reverse_result({}), ReverseGeocode)
        self.assertTrue(issubclass(LocationContextUnavailableError, ProviderUnavailableError))
