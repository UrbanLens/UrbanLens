"""Tests for services.import_formats.gpx.gpx_to_dict() - GPX waypoint import.

Tracks/routes are deliberately never imported as pins (see the module docstring
for the rationale), so the regression coverage here specifically checks that a
file containing both waypoints and a multi-point track only produces pins for
the waypoints.
"""
from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.import_formats.gpx import gpx_to_dict

_hyp = hyp_settings(max_examples=40, deadline=None)
_SAMPLE_DATA_DIR = Path(__file__).resolve().parents[5] / "sample_data"

_GPX_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  {body}
</gpx>
"""

_WPT_TEMPLATE = """<wpt lat="{lat}" lon="{lon}">
  <name>{name}</name>
  <desc>{desc}</desc>
</wpt>
"""

_TRACK = """<trk>
  <name>Recorded hike</name>
  <trkseg>
    <trkpt lat="1.0" lon="1.0"/>
    <trkpt lat="1.1" lon="1.1"/>
    <trkpt lat="1.2" lon="1.2"/>
  </trkseg>
</trk>
"""


def _wpt_xml(name: str, lat: float, lon: float, desc: str = "") -> str:
    return _WPT_TEMPLATE.format(name=name, lat=lat, lon=lon, desc=desc)


def _gpx_bytes(body: str) -> bytes:
    return _GPX_TEMPLATE.format(body=body).encode("utf-8")


class GpxToDictTests(TestCase):
    """gpx_to_dict() extracts only waypoints, never track/route points."""

    def setUp(self):
        self.profile = object()

    def test_single_waypoint(self):
        data = _gpx_bytes(_wpt_xml("Old Mill", 42.9013318, -73.3513978, "Abandoned mill"))

        pins = gpx_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "Old Mill")
        self.assertIn("Abandoned mill", pins[0]["description"])
        self.assertAlmostEqual(pins[0]["latitude"], 42.9013318)
        self.assertAlmostEqual(pins[0]["longitude"], -73.3513978)
        self.assertIs(pins[0]["profile"], self.profile)

    def test_track_points_are_not_imported(self):
        # A recorded hike can log a trackpoint every few seconds - importing
        # those as pins would flood the map, so tracks must be ignored entirely.
        body = _wpt_xml("Trailhead", 1.0, 1.0) + _TRACK
        data = _gpx_bytes(body)

        pins = gpx_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "Trailhead")

    def test_no_waypoints_returns_empty_list(self):
        data = _gpx_bytes(_TRACK)

        pins = gpx_to_dict(data, self.profile)

        self.assertEqual(pins, [])

    def test_elevation_and_time_folded_into_description(self):
        body = """<wpt lat="1.0" lon="2.0">
          <name>Marker</name>
          <ele>123.4</ele>
          <time>2020-01-01T00:00:00Z</time>
        </wpt>
        """
        data = _gpx_bytes(body)

        pins = gpx_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertIn("123.4", pins[0]["description"])
        self.assertIn("2020-01-01", pins[0]["description"])

    def test_real_world_sample_file(self):
        sample = (_SAMPLE_DATA_DIR / "sample.gpx").read_bytes()

        pins = gpx_to_dict(sample, self.profile)

        # sample.gpx has 7 waypoints and a separate 8-track/296-trackpoint
        # recording; only the waypoints should surface as pins.
        self.assertEqual(len(pins), 7)
        for pin in pins:
            self.assertIsInstance(pin["latitude"], float)
            self.assertIsInstance(pin["longitude"], float)

    @_hyp
    @given(
        name=st.text(
            alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="<>&\"'"),
            min_size=1,
            max_size=40,
        ).map(str.strip).filter(bool),
        lat=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
    )
    def test_round_trips_arbitrary_name_and_coordinates(self, name: str, lat: float, lon: float):
        data = _gpx_bytes(_wpt_xml(name, lat, lon))

        pins = gpx_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], name)
        self.assertAlmostEqual(pins[0]["latitude"], lat, places=6)
        self.assertAlmostEqual(pins[0]["longitude"], lon, places=6)
