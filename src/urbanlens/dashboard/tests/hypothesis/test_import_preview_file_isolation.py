"""However one uploaded file is malformed, it fails alone, and nothing off the globe reaches the preview.

The per-file guard catches ``IMPORT_PARSE_ERRORS``. Anything else a parser raises leaves the guard and fails the whole
preview as unreadable, every other file in the upload with it. A CSV cell past the csv module's field limit did that
(``csv.Error``), and so did a KML coordinate with no latitude (``IndexError``). A coordinate past the poles, or one
that is infinite or not a number, became a pin: JSON cannot hold the last two, so they broke the preview's result.
"""

from __future__ import annotations

import math
from typing import Any

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

_KML = (
    b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Dam</name>'
    b"<description>Old</description><Point><coordinates>-74.1,40.1,0</coordinates></Point></Placemark>"
    b"<Placemark><name>Road</name><LineString><coordinates>-74.1,40.1 -74.2,40.2</coordinates></LineString>"
    b"</Placemark><Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 1,0 1,1 0,0</coordinates>"
    b"</LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"
)
_GEOJSON = (
    b'{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": '
    b'[-74.1, 40.1]}, "properties": {"name": "Dam", "address": "1 Mill Rd"}}, {"type": "Feature", "geometry": '
    b'{"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, "properties": {"title": "Road"}}]}'
)
_CSV = b'name,latitude,longitude,description\n"Dam",40.1,-74.1,"Old dam"\nMill,40.2,-74.2,\n'
_WKT = b"POINT (1 2)\nLINESTRING (0 0, 2 2)\nPOLYGON ((0 0, 4 0, 4 2, 0 2, 0 0))\n"
_WKB = b"0101000000000000000000F03F000000000000F03F\n"
_OSM = (
    b'<?xml version="1.0"?><osm version="0.6"><node id="1" lat="40.1" lon="-74.1"><tag k="name" v="Dam"/></node>'
    b'<node id="2" lat="40.2" lon="-74.2"/><way id="9"><nd ref="1"/><nd ref="2"/><tag k="building" v="yes"/></way>'
    b"</osm>"
)
_GPX = (
    b'<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
    b'<wpt lat="40.1" lon="-74.1"><name>Dam</name><ele>12</ele><time>2019-01-01T00:00:00Z</time></wpt>'
    b'<trk><name>Walk</name><trkseg><trkpt lat="40.1" lon="-74.1"><time>2019-01-01T00:00:00Z</time></trkpt>'
    b'<trkpt lat="40.2" lon="-74.2"><time>2019-01-01T00:10:00Z</time></trkpt></trkseg></trk></gpx>'
)
_LOCATION_HISTORY = (
    b'{"timelineObjects": [{"placeVisit": {"location": {"latitudeE7": 401000000, "longitudeE7": -741000000, '
    b'"name": "Dam"}, "duration": {"startTimestamp": "2019-01-01T00:00:00Z", "endTimestamp": '
    b'"2019-01-01T01:00:00Z"}}}, {"activitySegment": {"startLocation": {"latitudeE7": 401000000, "longitudeE7": '
    b'-741000000}, "endLocation": {"latitudeE7": 402000000, "longitudeE7": -742000000}, "duration": '
    b'{"startTimestamp": "2019-01-01T01:00:00Z", "endTimestamp": "2019-01-01T02:00:00Z"}}}]}'
)
_MY_ACTIVITY = (
    b"<!DOCTYPE html><html><head><title>My Activity</title></head><body>"
    b'<div class="outer-cell"><div class="mdl-grid">'
    b'<div class="header-cell"><p class="mdl-typography--title">Maps<br></p></div>'
    b'<div class="content-cell mdl-typography--body-1">Directions to '
    b'<a href="https://www.google.com/maps/dir//40.1,-74.1/@40.1,-74.1,13z">Dam</a><br>'
    b"40.1,-74.1<br>Jul 3, 2021, 1:18:25 PM EDT<br></div></div></div></body></html>"
)

#: Each seed is a small file the preview reads, named as an upload of that format would be.
_SEEDS: dict[str, bytes] = {
    "places.kml": _KML,
    "Saved Places.json": _GEOJSON,
    "sites.csv": _CSV,
    "shapes.wkt": _WKT,
    "shapes.wkb": _WKB,
    "map.osm": _OSM,
    "walk.gpx": _GPX,
    "2019_JANUARY.json": _LOCATION_HISTORY,
    "MyActivity.html": _MY_ACTIVITY,
}

#: Fragments a malformed export plausibly holds: structure, separators, numbers at and past the edges, bad bytes.
_FRAGMENTS = st.sampled_from(
    [
        b"<",
        b">",
        b"/>",
        b'"',
        b"'",
        b",",
        b" ",
        b"\n",
        b"-",
        b".",
        b"[",
        b"]",
        b"{",
        b"}",
        b":",
        b"null",
        b"true",
        b"[]",
        b"{}",
        b"1e999",
        b"-1e999",
        b"99999999999999999999999",
        b"NaN",
        b"nan",
        b"inf",
        b"0x10",
        b"\x00",
        b"\xff",
        b"\xc3",
        b"\xef\xbb\xbf",
        b"&amp;",
        b"&x;",
        b"<![CDATA[",
        b"]]>",
        b"<coordinates>5</coordinates>",
        b"<nd/>",
        b'<nd ref=""/>',
        b'lat=""',
        b'lat="north"',
        b"POINT EMPTY",
        b"GEOMETRYCOLLECTION (",
        b'"coordinates": 5',
        b'"coordinates": [[]]',
        b'"geometry": []',
        b'"features": 7',
        b'"timelineObjects": {}',
        b'"latitudeE7": "x"',
        b"x" * 200_000,
    ]
)


#: Bytes that hold a file's structure together; rewriting one is how a value loses its other half.
_STRUCTURE = [b",", b" ", b".", b"-", b'"', b"<", b">", b"/", b"=", b":", b"[", b"]", b"{", b"}", b"\n"]


@st.composite
def _mutated(draw: Any) -> tuple[str, bytes]:
    name = draw(st.sampled_from(sorted(_SEEDS)))
    content = bytearray(_SEEDS[name])
    for _ in range(draw(st.integers(1, 4))):
        at = draw(st.integers(0, len(content)))
        operation = draw(st.sampled_from(["rewrite", "rewrite", "rewrite", "insert", "delete", "replace", "repeat"]))
        span = draw(st.integers(1, 12))
        present = [byte for byte in _STRUCTURE if byte in content]
        if operation == "rewrite" and present:
            byte = draw(st.sampled_from(present))
            places = [i for i in range(len(content)) if content[i : i + 1] == byte]
            place = places[at % len(places)]
            content[place : place + 1] = draw(
                st.sampled_from([b"", b" ", b"\n"]) | st.sampled_from(_STRUCTURE) | _FRAGMENTS
            )
        elif operation == "insert":
            content[at:at] = draw(_FRAGMENTS)
        elif operation == "delete":
            del content[at : at + span]
        elif operation == "replace":
            content[at : at + span] = draw(_FRAGMENTS)
        else:
            content[at:at] = content[at : at + span] * draw(st.integers(2, 6))
    return name, bytes(content)


def _assert_every_pin_is_on_the_globe(case: SimpleTestCase, parse: Any, label: str) -> None:
    for listed in parse.lists:
        for pin in listed["pins"]:
            on_globe = -90 <= pin["lat"] <= 90 and -180 <= pin["lng"] <= 180
            case.assertTrue(on_globe and math.isfinite(pin["lat"] + pin["lng"]), f"{label}: {pin}")


class OneMalformedFileFailsAloneTests(SimpleTestCase):
    @hyp_settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(upload=_mutated())
    def test_parsing_a_malformed_file_never_raises_past_the_per_file_guard(self, upload: tuple[str, bytes]) -> None:
        name, content = upload

        parse = GoogleMapsGateway(api_key="").parse_for_preview([(name, content)], Profile())

        _assert_every_pin_is_on_the_globe(self, parse, name)

    def test_no_single_structural_byte_rewritten_raises_past_the_per_file_guard(self) -> None:
        """Every structural byte of every seed, deleted, blanked or swapped: the edits random mutation rarely reaches."""
        gateway = GoogleMapsGateway(api_key="")
        for name, seed in _SEEDS.items():
            for at, byte in enumerate(seed):
                if bytes([byte]) not in _STRUCTURE:
                    continue
                for replacement in (b"", b" ", b"\n", b",", b'"', b"<", b"[", b"{", b"1e999", b"nan"):
                    content = seed[:at] + replacement + seed[at + 1 :]
                    label = f"{name}: byte {at} ({bytes([byte])!r} -> {replacement!r})"
                    try:
                        parse = gateway.parse_for_preview([(name, content)], Profile())
                    except Exception as exc:  # noqa: BLE001 - any escape is the failure
                        self.fail(f"{label} raised {exc!r}")
                    _assert_every_pin_is_on_the_globe(self, parse, label)

    def test_the_seeds_themselves_preview(self) -> None:
        """Anti-vacuity: each seed is read as its format, so a mutation starts from a file the preview takes."""
        for name, content in _SEEDS.items():
            with self.subTest(name=name):
                parse = GoogleMapsGateway(api_key="").parse_for_preview([(name, content)], Profile())

                self.assertEqual(parse.failed_formats, [])
                self.assertTrue(parse.previewed or any(parse.history.counts().values()), parse)

    def test_a_kml_coordinate_with_no_latitude_fails_only_its_file(self) -> None:
        bad = _KML.replace(b"-74.1,40.1,0", b"5")

        parse = GoogleMapsGateway(api_key="").parse_for_preview([("bad.kml", bad), ("sites.csv", _CSV)], Profile())

        self.assertEqual(parse.failed_formats, ["kml"])
        self.assertEqual(parse.previewed, 2)

    def test_a_place_off_the_globe_is_left_out_and_the_rest_kept(self) -> None:
        cases = {
            "infinite KML latitude": ("a.kml", _KML.replace(b"-74.1,40.1,0", b"-74.1,1e999,0"), 2),
            "KML latitude past the pole": ("a.kml", _KML.replace(b"-74.1,40.1,0", b"-74.1,95,0"), 2),
            "KML coordinates not a number": ("a.kml", _KML.replace(b"-74.1,40.1,0", b"nan,nan"), 2),
            "KML longitude past the antimeridian": ("a.kml", _KML.replace(b"-74.1,40.1,0", b"-190,40.1"), 2),
            "GeoJSON latitude past the pole": ("a.json", _GEOJSON.replace(b"[-74.1, 40.1]", b"[-74.1, 95]"), 1),
            "infinite CSV latitude": ("a.csv", _CSV.replace(b"40.1", b"inf"), 1),
            "CSV latitude not a number": ("a.csv", _CSV.replace(b"40.1", b"nan"), 1),
            "CSV latitude past the pole": ("a.csv", _CSV.replace(b"40.1", b"95"), 1),
            "infinite WKT coordinate": ("a.wkt", b"POINT (1 1e999)\nPOINT (1 2)\n", 1),
            "OSM node past the pole, its way's centroid on the globe": (
                "a.osm",
                _OSM.replace(b'lat="40.1"', b'lat="95"'),
                1,
            ),
            "GPX waypoint past the pole": ("a.gpx", _GPX.replace(b'<wpt lat="40.1"', b'<wpt lat="95"'), 0),
        }
        for label, (name, content, kept) in cases.items():
            with self.subTest(label):
                parse = GoogleMapsGateway(api_key="").parse_for_preview([(name, content)], Profile())

                _assert_every_pin_is_on_the_globe(self, parse, label)
                self.assertEqual(parse.failed_formats, [])
                self.assertEqual(parse.previewed, kept)
