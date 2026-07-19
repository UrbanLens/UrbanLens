"""Tests for services.import_formats.wkt_wkb - WKT/WKB pin import.

Unlike the other formats, a WKT/WKB file is N independent one-line records: a
malformed line must be skipped with a warning rather than aborting the whole
file, since these are typically hand-pasted rather than produced by a single
trusted export pipeline. That per-line fault tolerance is the main regression
risk covered here.
"""
from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings as hyp_settings, strategies as st
import shapely.geometry
import shapely.wkb
import shapely.wkt

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_formats.wkt_wkb import wkb_to_dict, wkt_to_dict

_hyp = hyp_settings(max_examples=40, deadline=None)
_SAMPLE_DATA_DIR = Path(__file__).resolve().parents[5] / "sample_data"


class WktToDictTests(SimpleTestCase):
    """wkt_to_dict() extracts one pin per valid geometry line."""

    def setUp(self):
        self.profile = object()

    def test_single_point(self):
        pins = wkt_to_dict(b"POINT (-73.78633 40.64596)", self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], -73.78633)
        self.assertAlmostEqual(pins[0]["latitude"], 40.64596)
        self.assertIs(pins[0]["profile"], self.profile)

    def test_polygon_uses_centroid(self):
        data = b"POLYGON ((0 0, 0 2, 2 2, 2 0, 0 0))"
        pins = wkt_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], 1.0)
        self.assertAlmostEqual(pins[0]["latitude"], 1.0)

    def test_multiple_lines_produce_multiple_pins(self):
        data = b"POINT (0 0)\nPOINT (1 1)\nPOINT (2 2)"
        pins = wkt_to_dict(data, self.profile)

        self.assertEqual(len(pins), 3)

    def test_invalid_line_skipped_without_aborting_file(self):
        data = b"POINT (0 0)\nNOT A GEOMETRY\nPOINT (1 1)"
        pins = wkt_to_dict(data, self.profile)

        self.assertEqual(len(pins), 2)

    def test_blank_and_comment_lines_ignored(self):
        data = b"\n# a comment\nPOINT (5 5)\n\n"
        pins = wkt_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)

    def test_empty_file_returns_empty_list(self):
        self.assertEqual(wkt_to_dict(b"", self.profile), [])

    def test_real_world_sample_file(self):
        sample = (_SAMPLE_DATA_DIR / "sample.wkt").read_bytes()

        pins = wkt_to_dict(sample, self.profile)

        # sample.wkt has one Point, one Polygon, and one LineString.
        self.assertEqual(len(pins), 3)

    @_hyp
    @given(
        lon=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
        lat=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
    )
    def test_round_trips_arbitrary_point(self, lon: float, lat: float):
        wkt_line = shapely.wkt.dumps(shapely.geometry.Point(lon, lat)).encode("utf-8")

        pins = wkt_to_dict(wkt_line, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], lon, places=5)
        self.assertAlmostEqual(pins[0]["latitude"], lat, places=5)


class WkbToDictTests(SimpleTestCase):
    """wkb_to_dict() handles both raw binary and hex-encoded WKB."""

    def setUp(self):
        self.profile = object()

    def test_raw_binary_single_geometry(self):
        data = shapely.wkb.dumps(shapely.geometry.Point(-73.78633, 40.64596))

        pins = wkb_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], -73.78633)
        self.assertAlmostEqual(pins[0]["latitude"], 40.64596)

    def test_hex_encoded_single_line(self):
        hex_line = shapely.wkb.dumps(shapely.geometry.Point(1.0, 2.0)).hex().encode("ascii")

        pins = wkb_to_dict(hex_line, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], 1.0)
        self.assertAlmostEqual(pins[0]["latitude"], 2.0)

    def test_hex_encoded_multiple_lines(self):
        lines = [shapely.wkb.dumps(shapely.geometry.Point(x, x)).hex() for x in (0.0, 1.0, 2.0)]
        data = "\n".join(lines).encode("ascii")

        pins = wkb_to_dict(data, self.profile)

        self.assertEqual(len(pins), 3)

    def test_invalid_hex_line_skipped(self):
        good = shapely.wkb.dumps(shapely.geometry.Point(1.0, 1.0)).hex()
        data = f"{good}\nnotvalidhex!!\n{good}".encode("ascii")

        pins = wkb_to_dict(data, self.profile)

        self.assertEqual(len(pins), 2)

    def test_real_world_sample_file(self):
        sample = (_SAMPLE_DATA_DIR / "sample.wkb").read_bytes()

        pins = wkb_to_dict(sample, self.profile)

        # sample.wkb has one Point, one Polygon, and one LineString, hex-encoded.
        self.assertEqual(len(pins), 3)
