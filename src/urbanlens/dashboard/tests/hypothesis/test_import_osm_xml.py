"""Tests for services.import_formats.osm_xml.osm_xml_to_dict() - OSM XML pin import.

Only tagged <node>/<way> elements should become pins - most nodes in a real
Overpass export are untagged geometry vertices belonging to a way, and importing
those too would flood the map with noise. That's the main regression risk
covered here, alongside way-centroid resolution.
"""
from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_formats.osm_xml import osm_xml_to_dict

_hyp = hyp_settings(max_examples=40, deadline=None)
_SAMPLE_DATA_DIR = Path(__file__).resolve().parents[5] / "sample_data"


def _osm_bytes(body: str) -> bytes:
    return f'<?xml version="1.0"?><osm version="0.6">{body}</osm>'.encode()


class OsmXmlToDictTests(SimpleTestCase):
    """osm_xml_to_dict() extracts pins from tagged nodes and ways only."""

    def setUp(self):
        self.profile = object()

    def test_tagged_node_becomes_pin(self):
        data = _osm_bytes(
            '<node id="1" lat="40.7262740" lon="-73.9744011">'
            '<tag k="name" v="Old Factory"/><tag k="abandoned" v="yes"/>'
            "</node>",
        )

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "Old Factory")
        self.assertIn("abandoned", pins[0]["description"])
        self.assertAlmostEqual(pins[0]["latitude"], 40.7262740)
        self.assertAlmostEqual(pins[0]["longitude"], -73.9744011)

    def test_untagged_node_ignored(self):
        data = _osm_bytes('<node id="1" lat="1" lon="2"/>')

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(pins, [])

    def test_tagged_way_resolves_centroid_from_referenced_nodes(self):
        body = (
            '<node id="1" lat="0" lon="0"/>'
            '<node id="2" lat="0" lon="2"/>'
            '<node id="3" lat="2" lon="2"/>'
            '<node id="4" lat="2" lon="0"/>'
            '<way id="10">'
            '<nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/>'
            '<tag k="building" v="yes"/><tag k="abandoned" v="yes"/>'
            "</way>"
        )
        data = _osm_bytes(body)

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["latitude"], 1.0)
        self.assertAlmostEqual(pins[0]["longitude"], 1.0)

    def test_way_missing_referenced_node_is_skipped(self):
        body = (
            '<node id="1" lat="0" lon="0"/>'
            '<way id="10"><nd ref="1"/><nd ref="999"/><tag k="building" v="yes"/></way>'
        )
        data = _osm_bytes(body)

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(pins, [])

    def test_untagged_way_ignored(self):
        body = '<node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/><way id="10"><nd ref="1"/><nd ref="2"/></way>'
        data = _osm_bytes(body)

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(pins, [])

    def test_node_without_name_tag_falls_back_to_generated_name(self):
        data = _osm_bytes('<node id="42" lat="1" lon="1"><tag k="historic" v="ruins"/></node>')

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertIn("42", pins[0]["name"])

    def test_real_world_sample_file(self):
        sample = (_SAMPLE_DATA_DIR / "sample.osm").read_bytes()

        pins = osm_xml_to_dict(sample, self.profile)

        # sample.osm has 6 tagged ways (abandoned buildings/piers/crane) and
        # 2 tagged standalone nodes (disused schools), all pulled from a real
        # Overpass query near NYC.
        self.assertEqual(len(pins), 8)
        names = {p["name"] for p in pins}
        self.assertIn("Saint Marys School", names)
        self.assertIn("Trinity Chapel School", names)

    @_hyp
    @given(
        lat=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
    )
    def test_round_trips_arbitrary_node_coordinates(self, lat: float, lon: float):
        data = _osm_bytes(f'<node id="1" lat="{lat!r}" lon="{lon!r}"><tag k="name" v="Spot"/></node>')

        pins = osm_xml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["latitude"], lat, places=6)
        self.assertAlmostEqual(pins[0]["longitude"], lon, places=6)
