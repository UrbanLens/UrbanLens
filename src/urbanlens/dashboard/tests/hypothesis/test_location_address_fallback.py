"""A Location Google cannot address still gets its municipality, county, state and country from OpenStreetMap.

k3s-staging's HRSH courtyard Location had no city, state or country, so its searches and its Wikipedia match
had no locality to lean on.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.locations.addresses import ensure_location_address

_ADMIN = {
    "country": "United States",
    "country_code": "us",
    "state": "New York",
    "county": "Dutchess County",
    "city": "Poughkeepsie",
    "postcode": "12601",
}


class NominatimAddressFallbackTests(TestCase):
    def _address(self, admin: dict | None) -> Location:
        location = baker.make(Location, latitude=41.73266, longitude=-73.92736, route=None, locality=None)
        with (
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", ""),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway.reverse_geocode_admin",
                return_value=admin,
            ),
        ):
            ensure_location_address(location)
        location.refresh_from_db()
        return location

    def test_without_google_the_administrative_fields_come_from_openstreetmap(self) -> None:
        location = self._address(_ADMIN)
        self.assertEqual(location.locality, "Poughkeepsie")
        self.assertEqual(location.administrative_area_level_1, "New York")
        self.assertEqual(location.administrative_area_level_2, "Dutchess County")
        self.assertEqual(location.zipcode, "12601")
        self.assertEqual(location.country, "United States")

    def test_the_street_is_never_taken_from_openstreetmap(self) -> None:
        """The nearest OSM way is as likely a campus service road as the postal street."""
        location = self._address(_ADMIN)
        self.assertFalse(location.route)

    def test_a_town_of_prefix_is_dropped(self) -> None:
        location = self._address({**_ADMIN, "city": "Town of Poughkeepsie"})
        self.assertEqual(location.locality, "Poughkeepsie")

    def test_nothing_found_writes_nothing(self) -> None:
        location = self._address(None)
        self.assertFalse(location.locality)
