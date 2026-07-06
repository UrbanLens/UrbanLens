"""Tests for GoogleMapsGateway._csv_row_iter() - CSV pin import.

Two independent CSV shapes are supported: Google Takeout exports (identified by
a URL column) and generic spreadsheet exports (Airtable, Google Sheets, Excel,
etc.) that carry their own latitude/longitude columns. This file only covers
the generic-column path; Takeout-URL parsing is exercised elsewhere via the
Google Maps import tests.
"""
from __future__ import annotations

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

_hyp = hyp_settings(max_examples=40, deadline=None)


class CsvRowIterLatLonTests(TestCase):
    """_csv_row_iter() falls back to explicit latitude/longitude columns when there's no URL column."""

    def setUp(self):
        self.profile = object()
        self.gateway = GoogleMapsGateway()

    def test_generic_latitude_longitude_columns(self):
        csv_text = "name,latitude,longitude,notes\nOld Mill,42.9013318,-73.3513978,Abandoned mill"

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "Old Mill")
        self.assertIn("Abandoned mill", pins[0]["description"])
        self.assertAlmostEqual(pins[0]["latitude"], 42.9013318)
        self.assertAlmostEqual(pins[0]["longitude"], -73.3513978)
        self.assertIsNone(pins[0]["cid"])
        self.assertIs(pins[0]["profile"], self.profile)

    def test_lat_lng_column_aliases(self):
        csv_text = "Name,Lat,Lng\nMy Place,1.5,2.5"

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["latitude"], 1.5)
        self.assertAlmostEqual(pins[0]["longitude"], 2.5)

    def test_url_column_takes_precedence_over_latlon_columns(self):
        # A row can theoretically have both; the Google Takeout URL is the more
        # authoritative source (it also carries the CID), so it wins.
        csv_text = 'URL,latitude,longitude\n"https://maps.google.com/maps/search/1.0,2.0",10,20'

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["latitude"], 1.0)
        self.assertAlmostEqual(pins[0]["longitude"], 2.0)

    def test_row_missing_coordinates_and_url_is_skipped(self):
        csv_text = "name,notes\nNo Coords,just some text"

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(pins, [None])

    def test_blank_row_is_silently_skipped(self):
        csv_text = "name,latitude,longitude\n,,"

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(pins, [])

    def test_coordinate_columns_excluded_from_description_fallback(self):
        # With no description-like column, pick_name_and_description() would
        # normally serialise every remaining column into the description - the
        # lat/lon columns must be excluded from that fallback.
        csv_text = "name,latitude,longitude\nOld Mill,42.9,-73.3"

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(pins[0]["description"], "")

    @_hyp
    @given(
        name=st.text(
            alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters=",\r\n\""),
            min_size=1,
            max_size=40,
        ).map(str.strip).filter(bool),
        lat=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
    )
    def test_round_trips_arbitrary_name_and_coordinates(self, name: str, lat: float, lon: float):
        csv_text = f"name,latitude,longitude\n{name},{lat},{lon}"

        pins = list(self.gateway._csv_row_iter(csv_text, self.profile))

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], name)
        self.assertAlmostEqual(pins[0]["latitude"], lat, places=6)
        self.assertAlmostEqual(pins[0]["longitude"], lon, places=6)
