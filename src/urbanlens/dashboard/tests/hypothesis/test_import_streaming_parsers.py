"""Each import parser that now reads a file a piece at a time reads what its whole-file predecessor read (P95).

The predecessors are kept here as the reference the streaming parsers must agree with, over generated documents.
``READ_CHUNK_BYTES`` is lowered so a few bytes make a chunk and every boundary a document has gets crossed.

Known, deliberate departures, which the generators steer clear of: ijson refuses JSON's non-standard ``NaN``
and ``Infinity`` and a number past a double's range, which ``json.loads`` accepted; a duplicated key no longer
keeps only its last value; a GeoJSON file with a byte-order mark now parses where it used to fail; a file's first
line is judged on its first 64 KiB; a file holding an integer outside the signed 64-bit range is re-read by
ijson's pure-Python parser, which takes any Unicode whitespace between tokens; a tagged OSM node whose id repeats
keeps its own coordinates rather than the last same-id node's; and a null Shapefile attribute is left out rather
than read as ``nan``.
"""

from __future__ import annotations

import datetime
import io
import itertools
import json
import math
from pathlib import Path
import struct
import tempfile
from typing import Any
from unittest import mock

from defusedxml.ElementTree import ParseError, fromstring as parse_xml_defused
import geopandas
import gpxpy
import gpxpy.gpx
import numpy as np
import pyogrio.errors
import shapely.wkb
import shapely.wkt

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.routes.model import RouteSource
from urbanlens.dashboard.services.apis.locations.google import my_activity
from urbanlens.dashboard.services.apis.locations.google.location_history import (
    parse_semantic_visits,
    semantic_history_to_routes,
)
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.import_export.archive_extractor import (
    _WKT_GEOMETRY_RE,
    _XML_TAG_FORMATS,
    _sniff_wkb,
    validate_content_type,
)
from urbanlens.dashboard.services.import_formats import osm_xml, shapefile, streams
from urbanlens.dashboard.services.import_formats.gpx_tracks import _build_route, read_gpx
from urbanlens.dashboard.services.import_formats.heuristics import (
    DEFAULT_LATITUDE_KEYS,
    DEFAULT_LONGITUDE_KEYS,
    normalize_header_key,
    pick_name_and_description,
)
from urbanlens.dashboard.services.import_formats.json_stream import iter_array_items, iter_geojson_features
from urbanlens.dashboard.services.import_formats.wkt_wkb import _pin_from_geometry, wkb_to_dict, wkt_to_dict
from urbanlens.dashboard.services.pins.history_import import _VISIT_KEYS, ImportedHistory, _encoded

_hyp = hyp_settings(max_examples=150, deadline=None)
_TINY_CHUNKS = mock.patch.object(streams, "READ_CHUNK_BYTES", 7)


def _cut(text: str, cuts: list[int]) -> list[str]:
    points = sorted({0, len(text), *(cut % (len(text) + 1) for cut in cuts)})
    return [text[start:end] for start, end in itertools.pairwise(points)]


def _outcome(call: Any) -> Any:
    """What *call* returns, or the class of what it raises, so a failure agrees as much as a result does."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the class is the outcome being compared
        return type(exc)


_LINE_TEXT = st.text(alphabet=st.sampled_from(list('ab ,"\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029\ufeffé')), max_size=60)


class LinesTests(SimpleTestCase):
    @_hyp
    @given(text=_LINE_TEXT, cuts=st.lists(st.integers(0, 60), max_size=8))
    def test_lines_split_as_splitlines_wherever_the_text_is_cut(self, text: str, cuts: list[int]) -> None:
        self.assertEqual(list(streams.iter_lines(_cut(text, cuts))), text.splitlines())

    @_hyp
    @given(text=_LINE_TEXT)
    def test_decoding_in_chunks_reads_the_whole_text(self, text: str) -> None:
        with _TINY_CHUNKS:
            self.assertEqual(
                "".join(streams.iter_decoded(io.BytesIO(text.encode()), "utf-8-sig")), text.encode().decode("utf-8-sig")
            )

    @_hyp
    @given(
        bom=st.booleans(),
        space=st.text(alphabet=st.sampled_from(list(" \t\n\r\x0b\x0c\x1c\x85\xa0\u2028\u3000")), max_size=20),
        body=st.text(max_size=20),
    )
    def test_skipping_leading_whitespace_lands_where_lstrip_would(self, bom: bool, space: str, body: str) -> None:
        data = (b"\xef\xbb\xbf" if bom else b"") + (space + body).encode()
        stream = io.BytesIO(data)
        with _TINY_CHUNKS:
            head = streams.skip_bom_and_whitespace(stream, limit=5)
        expected = data.decode("utf-8-sig").lstrip()
        self.assertEqual(head, expected[:5])
        self.assertEqual(stream.read().decode(), expected)


def _old_validate_content_type(name: str, data: bytes) -> str | None:
    """``validate_content_type`` before it read in chunks, less its logging."""
    if len(data) < 4:
        return None
    if _sniff_wkb(data):
        return "wkb"
    try:
        text = data.decode("utf-8-sig").lstrip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            if "features" in parsed:
                return "json"
            if "timelineObjects" in parsed:
                return "location_history"
        return None
    if text.startswith(("<?xml", "<kml", "<gpx", "<osm")):
        window = text[:2000]
        for tag, fmt in _XML_TAG_FORMATS:
            if tag in window:
                return fmt
        return None
    if text[:20].lower().startswith(("<!doctype html", "<html")):
        return "my_activity" if my_activity.looks_like_my_activity(text[:4000]) else None
    if _WKT_GEOMETRY_RE.match(text):
        return "wkt"
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) >= 10 and len(first_line) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in first_line):
        try:
            if _sniff_wkb(bytes.fromhex(first_line)):
                return "wkb"
        except ValueError:
            pass
    if any(h in first_line.lower() for h in ("url", "title", "note")):
        return "csv"
    columns = {normalize_header_key(column.strip('"')) for column in first_line.split(",")}
    if any(key in columns for key in DEFAULT_LATITUDE_KEYS) and any(key in columns for key in DEFAULT_LONGITUDE_KEYS):
        return "csv"
    return None


_JSON_SCALAR = (
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=4)
)
# Nested keys are never empty: ijson names a value under "" as it names the top level, so a "features" inside one
# would look like a top-level key - a document both readings take as holding nothing to import.
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(min_size=1, max_size=4), inner, max_size=3),
    max_leaves=6,
)
_JSON_DOCUMENT = st.one_of(
    st.dictionaries(
        st.sampled_from(["features", "timelineObjects", "type", "locations", "x"]), _JSON_VALUE, max_size=3
    ).map(json.dumps),
    st.lists(_JSON_VALUE, max_size=3).map(json.dumps),
    st.tuples(
        st.dictionaries(st.sampled_from(["features", "timelineObjects"]), _JSON_VALUE, min_size=1).map(json.dumps),
        st.integers(1, 30),
    ).map(lambda pair: pair[0][: -pair[1]]),
    st.sampled_from(['{"features": []} x', '{"features": [1,]}', "{'features': []}", "[", "{"]),
)
_OTHER_DOCUMENT = st.sampled_from(
    [
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"></kml>',
        "<gpx version='1.1'></gpx>",
        '<?xml version="1.0"?>\n<osm version="0.6"/>',
        '<?xml version="1.0"?><foo/>',
        '<!DOCTYPE html><html><p class="mdl-typography--title">Maps</p></html>',
        "<html><body>nothing</body></html>",
        "POINT (1 2)",
        "linestring z (1 2 3, 4 5 6)",
        "0101000000000000000000F03F000000000000F03F\n",
        "0101000000000000000000F03F000000000000F0\n",
        "Title,Note,URL,Comment\nA,,https://maps.google.com/x,\n",
        '"lat","lng","name"\n1,2,a\n',
        "name,latitude\nx,1\n",
        "just some words\nand more",
        "",
    ],
)


@st.composite
def _sniffable(draw: Any) -> bytes:
    body = draw(_JSON_DOCUMENT | _OTHER_DOCUMENT)
    space = draw(st.text(alphabet=st.sampled_from(list(" \t\n\r\x0c\xa0\u2028")), max_size=4))
    trailing = draw(st.sampled_from(["", " ", "\n", "\x0c", "\u2028", "x"]))
    data = (space + body + trailing).encode()
    if draw(st.booleans()):
        data = b"\xef\xbb\xbf" + data
    if draw(st.integers(0, 9)) == 0:
        data += b"\xff"
    if draw(st.integers(0, 9)) == 0:
        data = draw(st.binary(max_size=20))
    return data


class SniffAgreesWithTheWholeFileReadingTests(SimpleTestCase):
    @_hyp
    @given(data=_sniffable())
    def test_every_generated_file_sniffs_the_same(self, data: bytes) -> None:
        with _TINY_CHUNKS:
            self.assertEqual(validate_content_type("f", data), _old_validate_content_type("f", data))

    def test_the_stream_is_left_where_it_was(self) -> None:
        stream = io.BytesIO(b'{"features": []}')
        self.assertEqual(validate_content_type("f", stream), "json")
        self.assertEqual(stream.tell(), 0)


def _old_geojson(content: str) -> list[tuple[Any, ...]]:
    gateway = GoogleMapsGateway(api_key="")
    rows = []
    for feature in json.loads(content).get("features", []):
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        point = gateway._geojson_feature_point(geometry)
        if point is None:
            continue
        name, description = gateway._geojson_name_and_description(properties)
        rows.append((point[1], point[0], name, description))
    return rows


_COORD = st.tuples(st.floats(-179, 179, allow_nan=False), st.floats(-89, 89, allow_nan=False)).map(list)
_GEOMETRY = st.one_of(
    st.none(),
    st.builds(lambda c: {"type": "Point", "coordinates": c}, _COORD),
    st.builds(lambda cs: {"type": "LineString", "coordinates": cs}, st.lists(_COORD, min_size=2, max_size=4)),
    st.builds(lambda cs: {"type": "MultiPoint", "coordinates": cs}, st.lists(_COORD, min_size=1, max_size=3)),
    st.just({"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 2], [0, 2], [0, 0]]]}),
    st.just({"type": "Point", "coordinates": []}),
    st.just({"type": "Unknown"}),
)
_PROPERTIES = st.none() | st.dictionaries(
    st.sampled_from(["name", "description", "address", "title", "note", "x", "Name"]), _JSON_VALUE, max_size=4
)


class GeoJsonAgreesWithJsonLoadsTests(SimpleTestCase):
    @_hyp
    @given(
        features=st.lists(
            st.fixed_dictionaries({"type": st.just("Feature"), "geometry": _GEOMETRY, "properties": _PROPERTIES}),
            max_size=6,
        )
    )
    def test_every_generated_feature_collection_reads_the_same(self, features: list[dict[str, Any]]) -> None:
        content = json.dumps({"type": "FeatureCollection", "features": features})
        pins = GoogleMapsGateway(api_key="").iter_geojson_pins(content.encode(), user_profile=None)  # type: ignore[arg-type]
        self.assertEqual(
            [(p["latitude"], p["longitude"], p["name"], p["description"]) for p in pins], _old_geojson(content)
        )

    def test_an_integer_wider_than_64_bits_reads_as_json_loads_reads_it(self) -> None:
        point = {"type": "Point", "coordinates": [2, 1]}
        features = [{"geometry": point, "properties": {"name": f"n{i}", "id": 2**64 * (i == 2)}} for i in range(4)]
        content = json.dumps({"type": "FeatureCollection", "features": features})
        pins = GoogleMapsGateway(api_key="").iter_geojson_pins(content.encode(), user_profile=None)  # type: ignore[arg-type]
        self.assertEqual(
            [(p["latitude"], p["longitude"], p["name"], p["description"]) for p in pins], _old_geojson(content)
        )
        self.assertEqual(validate_content_type("f.json", content.encode()), "json")

    def test_a_byte_order_mark_is_read_past(self) -> None:
        content = b'\xef\xbb\xbf{"features": [{"geometry": {"type": "Point", "coordinates": [2, 1]}, "properties": {"name": "A"}}]}'
        pins = GoogleMapsGateway(api_key="").geojson_to_dict(content, user_profile=None)  # type: ignore[arg-type]
        self.assertEqual([(p["latitude"], p["longitude"], p["name"]) for p in pins], [(1, 2, "A")])

    def test_a_form_feed_between_tokens_is_not_whitespace_as_it_was_not(self) -> None:
        """yajl reads a vertical tab or form feed as whitespace; json.loads, and so the import, did not."""
        self.assertIsNone(validate_content_type("f.json", b'{"features": []}\x0c'))
        with self.assertRaises(ValueError):
            GoogleMapsGateway(api_key="").geojson_to_dict(b'{"features":\x0b[]}', user_profile=None)  # type: ignore[arg-type]

    def test_malformed_json_is_a_value_error_as_it_was(self) -> None:
        with self.assertRaises(ValueError):
            GoogleMapsGateway(api_key="").geojson_to_dict('{"features": [{"geometry": ', user_profile=None)  # type: ignore[arg-type]


_COORDINATE_NUMBER = st.integers(-(2**60), 2**60) | st.floats(-179, 179, allow_nan=False) | st.booleans()
_NESTED_COORDINATES = st.recursive(
    st.lists(_COORDINATE_NUMBER, max_size=4), lambda inner: st.lists(inner, max_size=3), max_leaves=12
)
_ANY_GEOMETRY = st.builds(
    lambda kind, coordinates: {"type": kind, "coordinates": coordinates},
    st.sampled_from(["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "LinearRing"]),
    _NESTED_COORDINATES | _JSON_SCALAR,
)
_ANY_GEOMETRY_OR_COLLECTION = _ANY_GEOMETRY | st.builds(
    lambda members: {"type": "GeometryCollection", "geometries": members}, st.lists(_ANY_GEOMETRY, max_size=3)
)


def _as_lists(value: Any) -> Any:
    """*value* with every array the compact reader made back as the lists ``json.loads`` builds."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _as_lists(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_lists(item) for item in value]
    return value


def _comparable(value: Any) -> Any:
    """A centroid of NaN equals another, as the test means it to."""
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    if isinstance(value, (list, tuple)):
        return [_comparable(item) for item in value]
    return value


class GeoJsonGeometriesReadCompactlyTests(SimpleTestCase):
    """P95: a geometry's positions arrive as one float array, and every pin is what the lists made."""

    @_hyp
    @given(document=_JSON_DOCUMENT)
    def test_any_document_builds_as_ijson_builds_it(self, document: str) -> None:
        raw = document.encode()

        self.assertEqual(
            _outcome(lambda: _as_lists(list(iter_geojson_features(io.BytesIO(raw))))),
            _outcome(lambda: list(iter_array_items(io.BytesIO(raw), "features"))),
        )

    @_hyp
    @given(geometries=st.lists(_ANY_GEOMETRY_OR_COLLECTION, max_size=4))
    def test_any_geometry_builds_as_ijson_builds_it(self, geometries: list[dict[str, Any]]) -> None:
        raw = json.dumps({"features": [{"geometry": geometry} for geometry in geometries]}).encode()

        self.assertEqual(
            _as_lists(list(iter_geojson_features(io.BytesIO(raw)))),
            list(iter_array_items(io.BytesIO(raw), "features")),
        )

    @_hyp
    @given(geometries=st.lists(_ANY_GEOMETRY_OR_COLLECTION, max_size=4))
    def test_any_geometry_makes_the_pin_the_lists_made(self, geometries: list[dict[str, Any]]) -> None:
        features = [{"geometry": geometry, "properties": {"name": f"n{i}"}} for i, geometry in enumerate(geometries)]
        content = json.dumps({"type": "FeatureCollection", "features": features})
        gateway = GoogleMapsGateway(api_key="")

        def new() -> list[tuple[Any, ...]]:
            pins = gateway.iter_geojson_pins(content.encode(), user_profile=None)  # type: ignore[arg-type]
            return [(pin["latitude"], pin["longitude"], pin["name"]) for pin in pins]

        self.assertEqual(
            _comparable(_outcome(new)), _comparable(_outcome(lambda: [row[:3] for row in _old_geojson(content)]))
        )

    def test_a_track_s_positions_are_one_float_array(self) -> None:
        raw = b'{"features": [{"geometry": {"type": "LineString", "coordinates": [[1, 2], [3.5, 4], [5, 6]]}}]}'

        (feature,) = iter_geojson_features(io.BytesIO(raw))
        coordinates = feature["geometry"]["coordinates"]

        self.assertIsInstance(coordinates, np.ndarray)
        self.assertEqual(coordinates.dtype, np.float64)
        self.assertEqual(coordinates.tolist(), [[1.0, 2.0], [3.5, 4.0], [5.0, 6.0]])

    def test_a_property_named_coordinates_is_left_as_lists(self) -> None:
        raw = b'{"features": [{"properties": {"coordinates": [[1, 2], [3, 4]]}}]}'

        (feature,) = iter_geojson_features(io.BytesIO(raw))

        self.assertEqual(feature["properties"]["coordinates"], [[1, 2], [3, 4]])


_TIMESTAMP = st.sampled_from(["2019-01-01T00:00:00Z", "2019-06-01T12:30:00.123Z", "not a time", ""]) | st.none()
_E7 = st.integers(-900_000_000, 900_000_000)
_PLACE_VISIT = st.fixed_dictionaries(
    {
        "placeVisit": st.fixed_dictionaries(
            {
                "location": st.fixed_dictionaries(
                    {"latitudeE7": _E7, "longitudeE7": _E7},
                    optional={"name": st.text(max_size=5), "placeId": st.text(max_size=5)},
                ),
                "duration": st.fixed_dictionaries({}, optional={"startTimestamp": _TIMESTAMP}),
            },
            optional={"visitConfidence": st.integers(0, 100)},
        ),
    },
)
_POINT = st.fixed_dictionaries({"latE7": _E7, "lngE7": _E7}, optional={"timestamp": _TIMESTAMP})
_ACTIVITY_SEGMENT = st.fixed_dictionaries(
    {
        "activitySegment": st.fixed_dictionaries(
            {
                "duration": st.fixed_dictionaries(
                    {}, optional={"startTimestamp": _TIMESTAMP, "endTimestamp": _TIMESTAMP}
                )
            },
            optional={
                "distance": st.integers(0, 50_000),
                "simplifiedRawPath": st.fixed_dictionaries({"points": st.lists(_POINT, max_size=4)}),
                "waypointPath": st.fixed_dictionaries(
                    {"waypoints": st.lists(st.fixed_dictionaries({"latE7": _E7, "lngE7": _E7}), max_size=4)}
                ),
            },
        ),
    },
)


class LocationHistoryAgreesWithJsonLoadsTests(SimpleTestCase):
    @_hyp
    @given(entries=st.lists(_PLACE_VISIT | _ACTIVITY_SEGMENT | st.just({"other": 1}), max_size=6))
    def test_every_generated_timeline_reads_the_same(self, entries: list[dict[str, Any]]) -> None:
        content = json.dumps({"timelineObjects": entries}).encode()
        profile = Profile()

        streamed = ImportedHistory()
        with _TINY_CHUNKS:
            streamed.add_location_history(content, profile, "2019_JANUARY.json")

        data = json.loads(content)
        whole = ImportedHistory(visits=[_encoded(visit, _VISIT_KEYS) for visit in parse_semantic_visits(data)])
        whole.add_routes(semantic_history_to_routes(data, profile, "2019_JANUARY.json"))
        self.assertEqual(streamed.to_json(), whole.to_json())

    def test_a_timeline_that_is_not_a_list_is_still_refused(self) -> None:
        for content in (b'{"timelineObjects": {}}', b'{"timelineObjects": 1}', b"[]"):
            with self.subTest(content=content), self.assertRaises(TypeError):
                ImportedHistory().add_location_history(content, Profile(), "x.json")

    def test_a_file_that_fails_partway_adds_nothing_to_the_preview(self) -> None:
        """It used to keep the visits read before the entry it failed on, while reporting the file as failed."""
        good = {
            "placeVisit": {
                "location": {"latitudeE7": 1, "longitudeE7": 2},
                "duration": {"startTimestamp": "2019-01-01T00:00:00Z"},
            }
        }
        content = json.dumps({"timelineObjects": [good, good, "not an entry"]}).encode()

        parse = GoogleMapsGateway(api_key="").parse_for_preview([("2019_JANUARY.json", content)], Profile())

        self.assertEqual(parse.failed_formats, ["location_history"])
        self.assertEqual(parse.history.counts()["visits"], 0)


class AnOversizedCsvCellFailsOnlyItsFileTests(SimpleTestCase):
    """csv refuses a cell over its 128 KiB field limit with ``csv.Error``, which no per-file handler named."""

    def test_the_other_files_in_the_upload_still_preview(self) -> None:
        oversized = b'name,latitude,longitude,description\n"Mill",40.5,-74.5,"' + b"long " * 40_000 + b'"\n'
        kml = (
            b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            b"<Placemark><name>Dam</name><Point><coordinates>-74.1,40.1</coordinates></Point></Placemark>"
            b"</Document></kml>"
        )

        parse = GoogleMapsGateway(api_key="").parse_for_preview([("notes.csv", oversized), ("dam.kml", kml)], Profile())

        self.assertEqual(parse.failed_formats, ["csv"])
        self.assertEqual([pin["name"] for listed in parse.lists for pin in listed["pins"]], ["Dam"])


_CSV_CELL = st.text(alphabet=st.sampled_from(list('ab1. ,"\n\r\u2028\ufeffé')), max_size=8)
_CSV_QUOTED = _CSV_CELL.map(lambda cell: '"' + cell.replace('"', '""') + '"')
_CSV_HEADER = st.sampled_from(
    [
        "name,latitude,longitude",
        "Title,Note,URL,Comment",
        "lat,lng,description",
        "\ufeffname,lat,lon",
        "\ufeff\ufeffLat,Long,Title",
    ]
)


class CsvAgreesWithTheWholeTextTests(SimpleTestCase):
    @_hyp
    @given(
        header=_CSV_HEADER,
        rows=st.lists(st.lists(_CSV_CELL | _CSV_QUOTED, min_size=1, max_size=4), max_size=5),
        newline=st.sampled_from(["\n", "\r\n", "\r"]),
    )
    def test_every_generated_csv_reads_the_same(self, header: str, rows: list[list[str]], newline: str) -> None:
        text = header + newline + newline.join(",".join(row) for row in rows)
        content = text.encode()
        gateway = GoogleMapsGateway(api_key="")

        whole = _outcome(lambda: list(gateway._csv_row_iter(content.decode("utf-8-sig"), None, offline=True)))  # type: ignore[arg-type]
        with _TINY_CHUNKS:
            lines = streams.iter_lines(streams.iter_decoded(io.BytesIO(content), "utf-8-sig"))
            streamed = _outcome(lambda: list(gateway._csv_row_iter(lines, None, offline=True)))  # type: ignore[arg-type]

        self.assertEqual(streamed, whole)


def _old_wkt(content: bytes) -> list[dict[str, Any]]:
    pins = []
    for number, raw in enumerate(content.decode("utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            geometry = shapely.wkt.loads(line)
        except (shapely.errors.ShapelyError, ValueError, TypeError):
            continue
        if (pin := _pin_from_geometry(geometry, number, "WKT", None)) is not None:  # type: ignore[arg-type]
            pins.append(pin)
    return pins


def _old_wkb(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError:
        pin = _pin_from_geometry(shapely.wkb.loads(content), 1, "binary WKB", None)  # type: ignore[arg-type]
        return [pin] if pin is not None else []
    pins = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            geometry = shapely.wkb.loads(bytes.fromhex(line))
        except (shapely.errors.ShapelyError, ValueError, TypeError):
            continue
        if (pin := _pin_from_geometry(geometry, number, "hex WKB", None)) is not None:  # type: ignore[arg-type]
            pins.append(pin)
    return pins


_WKT_LINE = st.sampled_from(
    [
        "POINT (1 2)",
        "  LINESTRING (0 0, 2 2)  ",
        "POLYGON ((0 0, 4 0, 4 2, 0 2, 0 0))",
        "POINT EMPTY",
        "# comment",
        "",
        "nonsense",
        "POINT (1",
    ]
)
_WKB_LINE = st.sampled_from(
    [
        "0101000000000000000000F03F000000000000F03F",
        "010200000002000000000000000000000000000000000000000000000000000040000000000000F03F",
        "zz",
        "0101",
        "# c",
        "",
    ]
)


class WktWkbAgreeWithTheWholeTextTests(SimpleTestCase):
    @_hyp
    @given(lines=st.lists(_WKT_LINE, max_size=6), newline=st.sampled_from(["\n", "\r\n", "\u2028"]))
    def test_every_generated_wkt_file_reads_the_same(self, lines: list[str], newline: str) -> None:
        content = newline.join(lines).encode()
        with _TINY_CHUNKS:
            self.assertEqual(wkt_to_dict(content, None), _old_wkt(content))  # type: ignore[arg-type]

    @_hyp
    @given(lines=st.lists(_WKB_LINE, max_size=6), newline=st.sampled_from(["\n", "\r\n"]))
    def test_every_generated_hex_wkb_file_reads_the_same(self, lines: list[str], newline: str) -> None:
        content = newline.join(lines).encode()
        with _TINY_CHUNKS:
            self.assertEqual(wkb_to_dict(content, None), _old_wkb(content))  # type: ignore[arg-type]

    def test_a_binary_wkb_file_is_still_one_geometry(self) -> None:
        content = struct.pack("<BIdd", 1, 1, 3.0, 4.0)
        self.assertEqual(wkb_to_dict(content, None), _old_wkb(content))  # type: ignore[arg-type]


def _old_osm(content: bytes) -> list[tuple[Any, ...]]:
    root = parse_xml_defused(content)
    tags_of = lambda element: {tag.get("k", ""): tag.get("v", "") for tag in element.findall("tag") if tag.get("k")}  # noqa: E731
    coords: dict[str, tuple[float, float]] = {}
    for node in root.findall("node"):
        node_id, lat, lon = node.get("id"), node.get("lat"), node.get("lon")
        if node_id is not None and lat is not None and lon is not None:
            coords[node_id] = (float(lat), float(lon))
    rows = []
    for node in root.findall("node"):
        tags = tags_of(node)
        if tags and node.get("id") in coords:
            lat, lon = coords[node.get("id")]
            rows.append((lat, lon, *pick_name_and_description(tags, fallback_name=f"OSM node {node.get('id')}")))
    for way in root.findall("way"):
        tags = tags_of(way)
        if not tags:
            continue
        refs = [nd.get("ref") for nd in way.findall("nd")]
        found = [coords[ref] for ref in refs if ref is not None and ref in coords]
        if not refs or len(found) != len(refs):
            continue
        rows.append(
            (
                sum(c[0] for c in found) / len(found),
                sum(c[1] for c in found) / len(found),
                *pick_name_and_description(tags, fallback_name=f"OSM way {way.get('id')}"),
            )
        )
    return rows


@st.composite
def _osm_document(draw: Any) -> bytes:
    parts = []
    # Ids 1-3 may be tagged and are unique; 4-7 are untagged vertices and may repeat.
    for node_id in [*range(1, draw(st.integers(0, 3)) + 1), *draw(st.lists(st.integers(4, 7), max_size=4))]:
        tags = "".join(
            f'<tag k="{k}" v="{v}"/>'
            for k, v in draw(
                st.dictionaries(
                    st.sampled_from(["name", "historic", "amenity"]), st.sampled_from(["a", "ruins", ""]), max_size=2
                )
            ).items()
            if node_id <= 3
        )
        lat = draw(st.sampled_from(['lat="1.5"', 'lat="-3"', "", 'lat="1.5"', 'lat="-3"', 'lat="north"']))
        parts.append(f'<node id="{node_id}" {lat} lon="2.25">{tags}</node>')
    for way_id in range(100, 100 + draw(st.integers(0, 3))):
        refs = "".join(f'<nd ref="{ref}"/>' for ref in draw(st.lists(st.integers(1, 8), max_size=4)))
        tag = '<tag k="building" v="yes"/>' if draw(st.booleans()) else ""
        parts.append(f'<way id="{way_id}">{refs}{tag}</way>')
    if draw(st.booleans()):
        parts.append('<relation id="7"><member type="way" ref="100"/><tag k="name" v="r"/></relation>')
    if draw(st.booleans()):
        draw(st.randoms()).shuffle(parts)
    return ('<?xml version="1.0"?><osm version="0.6">' + "".join(parts) + "</osm>").encode()


class OsmAgreesWithTheTreeTests(SimpleTestCase):
    @_hyp
    @given(content=_osm_document(), refs_per_pass=st.integers(1, 5))
    def test_every_generated_document_reads_the_same(self, content: bytes, refs_per_pass: int) -> None:
        def streamed() -> list[tuple[Any, ...]]:
            with mock.patch.object(osm_xml, "_WAY_REFS_PER_PASS", refs_per_pass):
                pins = osm_xml.osm_xml_to_dict(content, None)  # type: ignore[arg-type]
            return [(p["latitude"], p["longitude"], p["name"], p["description"]) for p in pins]

        self.assertEqual(_outcome(streamed), _outcome(lambda: _old_osm(content)))

    @_hyp
    @given(
        node_ids=st.lists(st.sampled_from(["7", "07", "-0", "0", "+7", "x", "-7", "\u0667"]), min_size=1, max_size=5),
        ref_ids=st.lists(
            st.sampled_from(["7", "07", "-0", "0", "+7", "x", "-7", "\u0667", ""]), min_size=1, max_size=4
        ),
    )
    def test_a_reference_finds_a_node_only_by_the_same_spelling(self, node_ids: list[str], ref_ids: list[str]) -> None:
        """Canonical integer ids are keyed as ints; every other spelling must still match only itself."""
        nodes = "".join(f'<node id="{node_id}" lat="{i}.5" lon="2"/>' for i, node_id in enumerate(node_ids))
        refs = "".join(f'<nd ref="{ref}"/>' for ref in ref_ids)
        content = f'<osm>{nodes}<way id="9">{refs}<tag k="name" v="w"/></way></osm>'.encode()

        streamed = _outcome(
            lambda: [(p["latitude"], p["longitude"]) for p in osm_xml.osm_xml_to_dict(content, None)]  # type: ignore[arg-type]
        )
        self.assertEqual(streamed, _outcome(lambda: [row[:2] for row in _old_osm(content)]))

    def test_a_way_s_children_below_its_own_are_not_its_references(self) -> None:
        content = (
            b'<osm><node id="1" lat="1" lon="2"/><node id="2" lat="3" lon="4"/>'
            b'<way id="9"><nd ref="1"/><extra><nd ref="2"/><tag k="x" v="y"/></extra><tag k="name" v="w"/></way></osm>'
        )

        pins = osm_xml.osm_xml_to_dict(content, None)  # type: ignore[arg-type]

        self.assertEqual([(p["latitude"], p["longitude"], p["name"]) for p in pins], [(1.0, 2.0, "w")])
        self.assertEqual(
            [(p["latitude"], p["longitude"], p["name"]) for p in pins], [row[:3] for row in _old_osm(content)]
        )

    def test_a_tagged_node_whose_id_repeats_keeps_its_own_coordinates(self) -> None:
        content = (
            b'<osm><node id="1" lat="1.5" lon="2"><tag k="name" v="a"/></node><node id="1" lat="-3" lon="2"/></osm>'
        )
        pins = osm_xml.osm_xml_to_dict(content, None)  # type: ignore[arg-type]
        self.assertEqual([(pin["latitude"], pin["longitude"]) for pin in pins], [(1.5, 2.0)])

    def test_a_malformed_document_still_fails(self) -> None:
        with self.assertRaises(ParseError):
            osm_xml.osm_xml_to_dict(b'<osm><node id="1" lat="1" lon="2"><tag k="name" v="a"/></node>', None)  # type: ignore[arg-type]

    def test_an_entity_is_still_refused(self) -> None:
        content = b'<?xml version="1.0"?><!DOCTYPE osm [<!ENTITY x "y">]><osm><node id="1" lat="1" lon="2"><tag k="name" v="&x;"/></node></osm>'
        with self.assertRaises(ValueError):
            osm_xml.osm_xml_to_dict(content, None)  # type: ignore[arg-type]


def _old_gpx(content: bytes, profile: Profile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``gpx_to_dict`` and ``gpx_tracks_to_routes`` as they were: a defusedxml screen, then gpxpy's own tree."""
    text = content.decode("utf-8")
    parse_xml_defused(text)
    gpx = gpxpy.parse(text)
    waypoints = []
    for waypoint in gpx.waypoints:
        parts = [part.strip() for part in (waypoint.description, waypoint.comment) if part and part.strip()]
        if waypoint.elevation is not None:
            parts.append(f"Elevation: {waypoint.elevation:.1f}m")
        if waypoint.time is not None:
            parts.append(f"Recorded: {waypoint.time.isoformat()}")
        waypoints.append(
            {
                "latitude": waypoint.latitude,
                "longitude": waypoint.longitude,
                "profile": profile,
                "name": (waypoint.name or "Unnamed waypoint").strip(),
                "description": " | ".join(parts),
            }
        )

    routes = []
    for track in gpx.tracks:
        points = [(p.latitude, p.longitude, p.time) for segment in track.segments for p in segment.points]
        climbs = [segment.get_uphill_downhill() for segment in track.segments]
        found = any(climb.uphill or climb.downhill for climb in climbs)
        gain = sum(climb.uphill or 0.0 for climb in climbs) if found else None
        loss = sum(climb.downhill or 0.0 for climb in climbs) if found else None
        built = _build_route(
            profile=profile,
            source=RouteSource.GPX_TRACK,
            source_filename="t.gpx",
            name=(track.name or "").strip(),
            points=[_raw(*p) for p in points],
            elevation_gain=gain,
            elevation_loss=loss,
        )
        if built:
            routes.append(built.to_json())
    for route in gpx.routes:
        points = [(p.latitude, p.longitude, p.time) for p in route.points]
        built = _build_route(
            profile=profile,
            source=RouteSource.GPX_ROUTE,
            source_filename="t.gpx",
            name=(route.name or "").strip(),
            points=[_raw(*p) for p in points],
        )
        if built:
            routes.append(built.to_json())
    return waypoints, routes


def _raw(lat: float, lng: float, time: Any) -> Any:
    from urbanlens.dashboard.services.import_formats.gpx_tracks import RawTrackPoint

    return RawTrackPoint(lat, lng, time)


_GPX_TIME = st.sampled_from(
    ["", "<time>2020-01-01T10:00:00Z</time>", "<time>2020-01-01T10:20:00.5+02:00</time>", "<time>bad</time>"]
)
_GPX_ELE = st.sampled_from(["", "<ele>12.5</ele>", "<ele> 3 </ele>", "<ele>-1</ele>"])


@st.composite
def _gpx_point(draw: Any, tag: str) -> str:
    lat, lon = draw(st.floats(-80, 80, allow_nan=False)), draw(st.floats(-170, 170, allow_nan=False))
    extras = draw(_GPX_ELE) + draw(_GPX_TIME)
    if tag == "wpt":
        extras += draw(st.sampled_from(["", "<name> Mill </name>", "<name/>"])) + draw(
            st.sampled_from(["", "<desc>old</desc>", "<cmt> x </cmt>"])
        )
    return f'<{tag} lat="{lat}" lon="{lon}">{extras}</{tag}>'


@st.composite
def _gpx_document(draw: Any) -> bytes:
    body = []
    for _ in range(draw(st.integers(0, 3))):
        body.append(draw(_gpx_point("wpt")))
    for index in range(draw(st.integers(0, 2))):
        segments = "".join(
            "<trkseg>" + "".join(draw(st.lists(_gpx_point("trkpt"), max_size=4))) + "</trkseg>"
            for _ in range(draw(st.integers(0, 2)))
        )
        body.append(f"<trk><name> Track {index} </name>{segments}</trk>")
    for _ in range(draw(st.integers(0, 2))):
        body.append(
            "<rte>"
            + draw(st.sampled_from(["", "<name>R</name>"]))
            + "".join(draw(st.lists(_gpx_point("rtept"), max_size=3)))
            + "</rte>"
        )
    draw(st.randoms()).shuffle(body)
    version = draw(st.sampled_from(["1.0", "1.1"]))
    namespace = draw(st.sampled_from(["", f' xmlns="http://www.topografix.com/GPX/{version.replace(".", "/")}"']))
    metadata = draw(
        st.sampled_from(["", "<metadata><name>m</name></metadata>" if version == "1.1" else "<name>m</name>"])
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="{version}" creator="t"{namespace}>{metadata}{"".join(body)}</gpx>'.encode()


class GpxAgreesWithGpxpyTests(SimpleTestCase):
    @_hyp
    @given(content=_gpx_document())
    def test_every_generated_document_reads_the_same(self, content: bytes) -> None:
        profile = Profile()

        def streamed() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            gpx = read_gpx(content, profile, "t.gpx")
            return gpx.waypoints, [route.to_json() for route in gpx.routes]

        self.assertEqual(_outcome(streamed), _outcome(lambda: _old_gpx(content, profile)))

    def test_a_field_gpxpy_refuses_still_fails_the_file(self) -> None:
        content = b'<gpx version="1.1"><trk><trkseg><trkpt lat="1" lon="2"><ele>high</ele></trkpt></trkseg></trk></gpx>'
        with self.assertRaises(gpxpy.gpx.GPXException):
            read_gpx(content, Profile(), "t.gpx")

    def test_an_entity_is_still_refused(self) -> None:
        content = b'<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY x "y">]><gpx version="1.1"><wpt lat="1" lon="2"><name>&x;</name></wpt></gpx>'
        with self.assertRaises(ValueError):
            read_gpx(content, Profile(), "t.gpx")


_DIRECTIONS_ENTRY = (
    '<div class="outer-cell mdl-cell"><div class="mdl-grid"><div class="header-cell"><p class="mdl-typography--title">Maps<br></p></div>'
    '<div class="content-cell mdl-cell mdl-typography--body-1">Directions to <a href="https://www.google.com/maps/dir//39.2,-84.5">Mill {i}</a>'
    "<br>39.2,-84.5{i}<br>Jul 3, 2026, 1:18:25 PM EDT<br></div></div></div>"
)


class MyActivityEntriesSplitAsTheWholeTextTests(SimpleTestCase):
    @_hyp
    @given(
        text=st.lists(st.sampled_from([*'<div class="outer-cel', "x", '<div class="outer-cell']), max_size=40).map(
            "".join
        ),
        cuts=st.lists(st.integers(0, 80), max_size=8),
    )
    def test_entries_split_as_str_split_wherever_the_text_is_cut(self, text: str, cuts: list[int]) -> None:
        self.assertEqual(list(my_activity._split_entries(_cut(text, cuts))), text.split(my_activity._ENTRY_BOUNDARY))

    def test_entries_read_in_tiny_chunks_are_the_entries_read_whole(self) -> None:
        content = (
            "<html><body>" + "".join(_DIRECTIONS_ENTRY.format(i=i) for i in range(20)) + "</body></html>"
        ).encode()

        whole = list(my_activity.parse_my_activity_entries(content))
        with _TINY_CHUNKS:
            streamed = list(my_activity.parse_my_activity_entries(content))

        self.assertEqual(len(whole), 20)
        self.assertEqual(streamed, whole)

    def test_a_file_that_is_not_utf8_at_its_end_yields_nothing_as_before(self) -> None:
        content = ("<html><body>" + _DIRECTIONS_ENTRY.format(i=1)).encode()
        self.assertEqual(len(list(my_activity.parse_my_activity_entries(content))), 1, "the premise failed")
        with _TINY_CHUNKS:
            self.assertEqual(list(my_activity.parse_my_activity_entries(content + b"\xff")), [])


def _old_shapefile(shp_path: Path, stem: str) -> list[dict[str, Any]]:
    frame = geopandas.read_file(shp_path)
    if frame.crs is not None and frame.crs.to_epsg() != 4326:
        frame = frame.to_crs(epsg=4326)
    pins = []
    for _, row in frame.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty or geometry.centroid.is_empty:
            continue
        properties = row.drop(labels="geometry").to_dict()
        name, description = pick_name_and_description(properties, fallback_name=f"{stem} feature")
        pins.append(
            {
                "latitude": geometry.centroid.y,
                "longitude": geometry.centroid.x,
                "name": name,
                "description": description,
            }
        )
    return pins


def _new_shapefile(shp_path: Path, stem: str) -> list[dict[str, Any]]:
    with mock.patch.object(shapefile, "_BATCH_FEATURES", 3):
        pins = list(shapefile.iter_shapefile_pins(shp_path, stem, None))  # type: ignore[arg-type]
    return [{key: pin[key] for key in ("latitude", "longitude", "name", "description")} for pin in pins]


_DBF_FIELDS = [("name", "C", 10, 0), ("n", "N", 10, 0), ("f", "N", 10, 3), ("l", "L", 1, 0), ("d", "D", 8, 0)]


def _dbf(records: list[tuple[bool, list[bytes]]]) -> bytes:
    """A dBASE III table of ``_DBF_FIELDS``, each record ``(deleted, [value per field])``, blank values null."""
    record_size = 1 + sum(width for _, _, width, _ in _DBF_FIELDS)
    table = struct.pack("<BBBBIHH20x", 3, 126, 10, 2, len(records), 33 + 32 * len(_DBF_FIELDS), record_size)
    for name, kind, width, decimals in _DBF_FIELDS:
        table += struct.pack("<11sc4xBB14x", name.encode(), kind.encode(), width, decimals)
    table += b"\r"
    for deleted, values in records:
        table += b"*" if deleted else b" "
        for (_, kind, width, _), value in zip(_DBF_FIELDS, values, strict=True):
            table += value.ljust(width) if kind == "C" else value.rjust(width)
    return table + b"\x1a"


def _write_shapefile(
    directory: str, points: list[tuple[float, float] | None], table: bytes, *, crs: str, cpg: bool
) -> Path:
    shp_path = Path(directory) / "places.shp"
    geometry = [None if point is None else shapely.Point(point) for point in points]
    geopandas.GeoDataFrame({"z": range(len(points))}, geometry=geometry, crs=crs).to_file(shp_path)
    shp_path.with_suffix(".dbf").write_bytes(table)
    if not cpg:
        shp_path.with_suffix(".cpg").unlink()
    return shp_path


@st.composite
def _dbf_record(draw: Any, encoding: str) -> tuple[bool, list[bytes]]:
    return draw(st.booleans()), [
        ("x" + draw(st.text(alphabet=st.sampled_from(list("ab é")), max_size=4))).encode(encoding),
        str(draw(st.integers(-99999, 999999))).encode(),
        f"{draw(st.integers(-99999, 99999)) / 1000:.3f}".encode(),
        draw(st.sampled_from([b"T", b"F", b"Y", b"N"])),
        draw(st.dates(datetime.date(1900, 1, 1), datetime.date(2100, 12, 31))).strftime("%Y%m%d").encode(),
    ]


@st.composite
def _shapefile_contents(
    draw: Any,
) -> tuple[list[tuple[float, float] | None], list[tuple[bool, list[bytes]]], str, bool]:
    cpg = draw(st.booleans())
    crs = draw(st.sampled_from(["EPSG:4326", "EPSG:3857"]))
    bound = 170.0 if crs == "EPSG:4326" else 1.5e7
    point = st.tuples(st.floats(-bound, bound), st.floats(-bound / 2, bound / 2))
    records = draw(st.lists(_dbf_record("utf-8" if cpg else "latin-1"), min_size=1, max_size=12))
    points = draw(st.lists(point | st.none(), min_size=len(records), max_size=len(records)))
    return points, records, crs, cpg


class ShapefileAgreesWithTheWholeFrameTests(SimpleTestCase):
    """A Shapefile read a batch at a time reads as the whole GeoDataFrame did, wherever no attribute is null."""

    @_hyp
    @given(contents=_shapefile_contents())
    def test_every_generated_shapefile_reads_the_same(
        self, contents: tuple[list[tuple[float, float] | None], list[tuple[bool, list[bytes]]], str, bool]
    ) -> None:
        points, records, crs, cpg = contents
        with tempfile.TemporaryDirectory() as directory:
            shp_path = _write_shapefile(directory, points, _dbf(records), crs=crs, cpg=cpg)
            self.assertEqual(_new_shapefile(shp_path, "places"), _old_shapefile(shp_path, "places"))

    def test_a_record_marked_deleted_is_skipped_once_whatever_batch_it_falls_in(self) -> None:
        records = [(index in (2, 3), [f"n{index}".encode(), b"1", b"1.000", b"T", b"20200101"]) for index in range(8)]
        with tempfile.TemporaryDirectory() as directory:
            shp_path = _write_shapefile(
                directory, [(index, index) for index in range(8)], _dbf(records), crs="EPSG:4326", cpg=True
            )
            names = [pin["name"] for pin in _new_shapefile(shp_path, "places")]

        self.assertEqual(names, ["n0", "n1", "n4", "n5", "n6", "n7"])

    def test_a_null_attribute_is_left_out_rather_than_read_as_nan(self) -> None:
        """Where the whole frame differed: it read a null text field as ``nan``, and an integer column holding
        a null as floats."""
        records = [(False, [b"", b"", b"", b"?", b""]), (False, [b"", b"7", b"", b"?", b""])]
        with tempfile.TemporaryDirectory() as directory:
            shp_path = _write_shapefile(directory, [(1, 1), (2, 2)], _dbf(records), crs="EPSG:4326", cpg=True)
            pins = _new_shapefile(shp_path, "places")

        self.assertEqual(
            [(pin["name"], pin["description"]) for pin in pins], [("places feature", ""), ("places feature", "n: 7")]
        )

    @_hyp
    @given(cut=st.integers(0, 400))
    def test_a_truncated_shp_reads_as_the_whole_frame_did(self, cut: int) -> None:
        records = [(False, [f"n{index}".encode(), b"1", b"1.000", b"T", b"20200101"]) for index in range(8)]
        with tempfile.TemporaryDirectory() as directory:
            shp_path = _write_shapefile(
                directory, [(index, index) for index in range(8)], _dbf(records), crs="EPSG:4326", cpg=True
            )
            shp_path.write_bytes(shp_path.read_bytes()[:cut])
            new = _outcome(lambda: _new_shapefile(shp_path, "places"))
            old = _outcome(lambda: _old_shapefile(shp_path, "places"))

        if isinstance(old, list):
            self.assertEqual(new, old)
        else:
            self.assertTrue(issubclass(new, (OSError, ValueError, pyogrio.errors.DataSourceError)), new)
