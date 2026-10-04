"""What reading one import preview file costs in memory, per format (P95).

A preview entry may be up to 1 GB, and media-worker parses two at once in 3 GB, so no parse may hold a structure
built from the whole file. Each test reads a 12 MiB file and holds the parse's tracemalloc peak under an eighth of
it. ``MAX_PREVIEW_PINS`` is lowered so the pins a preview keeps are not what is measured.
"""

from __future__ import annotations

from collections.abc import Callable
import io
import json
import os
import tempfile
import tracemalloc
from typing import Any
from unittest import mock
import zipfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
import geopandas
from model_bakery import baker
import shapely

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.pins import import_preview

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"

_SIZE = 12 * 1024 * 1024
_KEPT_PINS = 50


def _filled(head: str, item: Callable[[int], str], tail: str) -> bytes:
    """``item(0)``, ``item(1)``, ... between *head* and *tail*, until the file reaches ``_SIZE``."""
    parts = [head]
    total = len(head)
    index = 0
    while total < _SIZE:
        part = item(index)
        parts.append(part)
        total += len(part)
        index += 1
    parts.append(tail)
    return "".join(parts).encode()


def _lat(i: int) -> float:
    return 40 + (i % 1000) / 1000


def _lng(i: int) -> float:
    return -74 - (i % 997) / 1000


_KML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'


def _kml_placemark(i: int) -> str:
    return (
        f"<Placemark><name>Mill {i}</name><description><![CDATA[An old mill<br>https://example.com/{i}]]></description>"
        f"<styleUrl>#icon-1899</styleUrl><Point><coordinates>{_lng(i)},{_lat(i)},0</coordinates></Point></Placemark>\n"
    )


def _kml() -> bytes:
    return _filled(_KML_HEAD, _kml_placemark, "</Document></kml>\n")


def _kml_one_element(geometry: str) -> bytes:
    """A KML holding one placemark, whose geometry is the whole file: a long track, or a large area's outline."""
    open_tag, close_tag = {
        "LineString": ("<LineString><coordinates>\n", "\n</coordinates></LineString>"),
        "Polygon": (
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>\n",
            "\n</coordinates></LinearRing></outerBoundaryIs></Polygon>",
        ),
    }[geometry]
    return _filled(
        _KML_HEAD + "<Placemark><name>The long walk</name>" + open_tag,
        lambda i: f"{_lng(i):.6f},{_lat(i // 997):.6f},0 ",
        close_tag + "</Placemark></Document></kml>\n",
    )


def _geojson_one_element(geometry: str) -> bytes:
    """A GeoJSON holding one feature, whose geometry is the whole file: a long track, or a large area's outline."""
    first = f"[{_lng(0):.6f}, {_lat(0):.6f}]"
    opening, closing = {"LineString": ("[", "]"), "Polygon": ("[[", "]]")}[geometry]
    return _filled(
        '{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "The long walk"}, '
        f'"geometry": {{"type": "{geometry}", "coordinates": {opening}\n',
        lambda i: f"[{_lng(i):.6f}, {_lat(i // 997):.6f}], ",
        f"{first}{closing}}}}}]}}\n",
    )


def _kml_styles_first() -> bytes:
    """A KML whose placemarks follow a file's worth of shared styles, as a My Maps export's do."""
    return _filled(
        _KML_HEAD,
        lambda i: (
            f'<Style id="icon-{i}"><IconStyle><scale>1</scale><Icon><href>https://example.com/{i}.png</href></Icon>'
            "</IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>\n"
        ),
        "".join(_kml_placemark(i) for i in range(_KEPT_PINS)) + "</Document></kml>\n",
    )


def _geojson() -> bytes:
    def feature(i: int) -> str:
        properties = {"name": f"Mill {i}", "address": f"{i} Mill Rd, Albany, NY", "date": "2019-05-01T12:00:00Z"}
        return (
            json.dumps(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [_lng(i), _lat(i)]},
                    "properties": properties,
                }
            )
            + ",\n"
        )

    return _filled(
        '{"type": "FeatureCollection", "features": [\n', feature, '{"type": "Feature", "geometry": null}]}\n'
    )


def _records() -> bytes:
    """Google Takeout's raw Records.json: valid JSON the preview does not import, so it is sniffed and refused."""

    def location(i: int) -> str:
        return (
            json.dumps(
                {
                    "latitudeE7": i,
                    "longitudeE7": -i,
                    "accuracy": 12,
                    "source": "WIFI",
                    "timestamp": "2019-01-01T00:00:00Z",
                }
            )
            + ",\n"
        )

    return _filled('{"locations": [\n', location, '{"latitudeE7": 0, "longitudeE7": 0}]}\n')


def _location_history() -> bytes:
    """Semantic Location History, all of it what the import drops: unconfident visits and their candidates."""

    def visit(i: int) -> str:
        candidate = {
            "latitudeE7": 407000000,
            "longitudeE7": -740000000,
            "placeId": f"ChIJabc{i}",
            "semanticType": "TYPE_UNKNOWN",
        }
        place = {
            "location": {
                "latitudeE7": 407000000,
                "longitudeE7": -740000000,
                "name": f"Mill {i}",
                "address": f"{i} Mill Rd",
            },
            "duration": {"startTimestamp": "2019-01-01T00:00:00Z", "endTimestamp": "2019-01-01T01:00:00Z"},
            "visitConfidence": 10,
            "otherCandidateLocations": [candidate] * 6,
        }
        return json.dumps({"placeVisit": place}) + ",\n"

    return _filled('{"timelineObjects": [\n', visit, '{"placeVisit": {}}]}\n')


def _csv() -> bytes:
    return _filled(
        "name,latitude,longitude,description\n",
        lambda i: f'"Mill {i}",{_lat(i)},{_lng(i)},"An old mill by the river, {i}"\n',
        "",
    )


def _wkt() -> bytes:
    return _filled("", lambda i: f"POINT ({_lng(i)} {_lat(i)})\nPOLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))\n", "")


def _wkb() -> bytes:
    # A little-endian WKB point at (1, 1), hex-encoded.
    return _filled("", lambda _: "0101000000000000000000F03F000000000000F03F\n", "")


_OSM_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n'


def _osm() -> bytes:
    return _filled(
        _OSM_HEAD,
        lambda i: (
            f'<node id="{i}" lat="{_lat(i)}" lon="{_lng(i)}" version="1"><tag k="name" v="Mill {i}"/><tag k="historic" v="ruins"/></node>\n'
        ),
        "</osm>\n",
    )


def _osm_ways() -> bytes:
    """An OSM extract as most are: untagged vertices, then the tagged ways they make up."""
    return _filled(
        _OSM_HEAD,
        lambda i: f'<node id="{i}" lat="{_lat(i)}" lon="{_lng(i)}" version="1"/>\n',
        "".join(
            f'<way id="{i}" version="1"><nd ref="{2 * i}"/><nd ref="{2 * i + 1}"/><tag k="building" v="ruins"/></way>\n'
            for i in range(_KEPT_PINS)
        )
        + "</osm>\n",
    )


def _osm_one_way() -> bytes:
    """An OSM extract that is one tagged way through every node it holds: a long route, or a large area's outline."""
    count = _SIZE // 72
    nodes = "".join(f'<node id="{1000 + i}" lat="{_lat(i)}" lon="{_lng(i)}"/>\n' for i in range(count))
    refs = "".join(f'<nd ref="{1000 + i}"/>' for i in range(count))
    return f'{_OSM_HEAD}{nodes}<way id="1">{refs}<tag k="highway" v="track"/></way>\n</osm>\n'.encode()


def _wkt_one_line() -> bytes:
    """A WKT file that is one line: a long track."""
    return _filled("LINESTRING (", lambda i: f"{_lng(i):.6f} {_lat(i // 997):.6f}, ", f"{_lng(0):.6f} {_lat(0):.6f})\n")


def _wkb_one_line() -> bytes:
    """A hex WKB file that is one line: a long track."""
    count = _SIZE // 32
    track = shapely.LineString([(_lng(i), _lat(i // 997)) for i in range(count)])
    return shapely.to_wkb(track, hex=True).encode() + b"\n"


def _gpx_waypoints() -> bytes:
    return _filled(
        '<?xml version="1.0"?>\n<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">\n',
        lambda i: (
            f'<wpt lat="{_lat(i)}" lon="{_lng(i)}"><ele>12.5</ele><name>Mill {i}</name><desc>An old mill</desc></wpt>\n'
        ),
        "</gpx>\n",
    )


def _my_activity() -> bytes:
    """A My Activity export that is all searches, which the import skips."""
    return _filled(
        '<!DOCTYPE html><html><head><title>My Activity</title></head><body><p class="mdl-typography--title">Maps</p>',
        lambda i: (
            '<div class="outer-cell mdl-cell mdl-cell--12-col"><div class="mdl-grid"><div class="header-cell mdl-cell">'
            '<p class="mdl-typography--title">Search<br></p></div><div class="content-cell mdl-cell mdl-typography--body-1">'
            f'Searched for <a href="https://www.google.com/search?q=mill+{i}">mill {i}</a><br>Jul 3, 2021, 1:18:25 PM EDT<br></div></div></div>'
        ),
        "</body></html>",
    )


def _shapefile() -> tuple[bytes, int]:
    """A zipped point Shapefile, and how large its parts are unzipped."""
    count = _SIZE // 250
    frame = geopandas.GeoDataFrame(
        {
            "name": [f"Mill {i}" for i in range(count)],
            "descr": [f"An old mill by the river, {i:0>190}" for i in range(count)],
        },
        geometry=[shapely.Point(_lng(i), _lat(i)) for i in range(count)],
        crs="EPSG:4326",
    )
    buf = io.BytesIO()
    size = 0
    with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        frame.to_file(os.path.join(directory, "places.shp"))
        for name in sorted(os.listdir(directory)):
            zf.write(os.path.join(directory, name), name)
            size += os.path.getsize(os.path.join(directory, name))
    return buf.getvalue(), size


def _zipped(name: str, content: bytes, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    return _zipped_all([(name, content)], compression)


def _zipped_all(entries: list[tuple[str, bytes]], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


class _MemoryCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(single_flight.release, import_preview.parse_slot_key(0))
        self.profile = baker.make(User).profile
        self.addCleanup(single_flight.release, import_preview.guard_key(self.profile.pk))

    def _peak(self, name: str, upload: bytes) -> tuple[int, dict[str, Any]]:
        """Read *upload* as a preview, returning the parse's tracemalloc peak and what the dialog would be told."""
        with mock.patch(ENQUEUE, return_value=mock.Mock()):
            job_id = import_preview.start_import_preview(self.profile, [SimpleUploadedFile(name, upload)])
        self.addCleanup(import_preview.shutil.rmtree, import_preview.job_dir(job_id), True)

        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(ENQUEUE, return_value=mock.Mock()),
            mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", _KEPT_PINS),
        ):
            tracemalloc.start()
            try:
                import_preview.parse_import_preview(self.profile.pk, job_id)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        return peak, import_preview.read_preview(self.profile.user_id, job_id) or {}

    def assert_read_in_pieces(
        self, name: str, upload: bytes, *, size: int | None = None, fills_the_preview: bool = True
    ) -> None:
        """Read *upload*, holding the parse under an eighth of *size*, the bytes it unpacks to (its own by default)."""
        size = len(upload) if size is None else size
        peak, state = self._peak(name, upload)
        if fills_the_preview:
            self.assertEqual(state.get("status"), "done", state)
            self.assertEqual(
                state["result"]["total"], _KEPT_PINS, "the premise failed: the file did not fill the preview"
            )
        else:
            self.assertEqual(
                state.get("status"), "error", f"the premise failed: the file held something to import: {state}"
            )
        self.assertLess(peak, size // 8, f"reading a {size:,}-byte {name} peaked at {peak:,} bytes")


class PinFormatsAreReadInPiecesTests(_MemoryCase):
    """Each pin format stops reading once the preview is full, holding a few chunks of the file meanwhile."""

    def test_kml(self) -> None:
        self.assert_read_in_pieces("places.kml", _kml())

    def test_kml_whose_styles_come_first(self) -> None:
        self.assert_read_in_pieces("places.kml", _kml_styles_first())

    def test_geojson(self) -> None:
        self.assert_read_in_pieces("Saved Places.json", _geojson())

    def test_csv(self) -> None:
        self.assert_read_in_pieces("sites.csv", _csv())

    def test_wkt(self) -> None:
        self.assert_read_in_pieces("shapes.wkt", _wkt())

    def test_wkb(self) -> None:
        self.assert_read_in_pieces("shapes.wkb", _wkb())

    def test_osm_xml(self) -> None:
        self.assert_read_in_pieces("map.osm", _osm())

    def test_osm_xml_ways(self) -> None:
        self.assert_read_in_pieces("map.osm", _osm_ways())

    def test_gpx_waypoints(self) -> None:
        self.assert_read_in_pieces("track.gpx", _gpx_waypoints())

    def test_shapefile(self) -> None:
        upload, size = _shapefile()
        self.assert_read_in_pieces("places.zip", upload, size=size)


class OneLargeElementTests(_MemoryCase):
    """P95: a file that is one element is read with no per-coordinate objects, only the element's own text."""

    def assert_held_near_its_text(self, name: str, upload: bytes) -> None:
        peak, state = self._peak(name, upload)

        self.assertEqual(state.get("status"), "done", state)
        self.assertEqual(state["result"]["total"], 1, state)
        self.assertLess(peak, 3 * len(upload), f"reading a {len(upload):,}-byte {name} peaked at {peak:,} bytes")

    def test_a_kml_track(self) -> None:
        self.assert_held_near_its_text("track.kml", _kml_one_element("LineString"))

    def test_a_kml_outline(self) -> None:
        self.assert_held_near_its_text("outline.kml", _kml_one_element("Polygon"))

    def test_a_geojson_track(self) -> None:
        self.assert_held_near_its_text("track.geojson", _geojson_one_element("LineString"))

    def test_a_geojson_outline(self) -> None:
        self.assert_held_near_its_text("outline.geojson", _geojson_one_element("Polygon"))

    def test_an_osm_way(self) -> None:
        self.assert_held_near_its_text("route.osm", _osm_one_way())

    def test_a_wkt_line(self) -> None:
        self.assert_held_near_its_text("track.wkt", _wkt_one_line())

    def test_a_hex_wkb_line(self) -> None:
        self.assert_held_near_its_text("track.wkb", _wkb_one_line())


class HistoryFormatsAreReadInPiecesTests(_MemoryCase):
    """A history file is read to its end, holding what the import keeps - here nothing - but not the file."""

    def test_location_history(self) -> None:
        self.assert_read_in_pieces("2019_JANUARY.json", _location_history(), fills_the_preview=False)

    def test_my_activity(self) -> None:
        self.assert_read_in_pieces("MyActivity.html", _my_activity(), fills_the_preview=False)


class AFileThePreviewRefusesIsNotBuiltTests(_MemoryCase):
    def test_raw_location_records_are_sniffed_without_being_parsed_whole(self) -> None:
        self.assert_read_in_pieces("Records.json", _records(), fills_the_preview=False)


class AnArchiveEntryIsNotReadWholeTests(_MemoryCase):
    """An entry is extracted to disk a chunk at a time, so its 1 GB cap bounds disk, not memory."""

    def test_a_kml_entry_in_a_zip(self) -> None:
        content = _kml()
        self.assert_read_in_pieces("export.zip", _zipped("places.kml", content), size=len(content))

    def test_a_kmz(self) -> None:
        content = _kml()
        self.assert_read_in_pieces("places.kmz", _zipped("doc.kml", content), size=len(content))

    def test_a_stored_entry_whose_archive_is_as_large_as_it_is(self) -> None:
        content = _csv()
        self.assert_read_in_pieces("export.zip", _zipped("sites.csv", content, zipfile.ZIP_STORED), size=len(content))

    def test_an_entry_is_extracted_beside_the_upload_and_removed(self) -> None:
        """media-worker's /tmp is a tmpfs, whose pages count against its memory limit."""
        written: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            handle, path = real_mkstemp(*args, **kwargs)
            written.append(path)
            return handle, path

        with mock.patch(
            "urbanlens.dashboard.services.import_export.archive_extractor.tempfile.mkstemp", side_effect=mkstemp
        ):
            self._peak(
                "export.zip", _zipped("Saved.json", json.dumps({"type": "FeatureCollection", "features": []}).encode())
            )

        self.assertTrue(written, "the premise failed: nothing was extracted")
        for path in written:
            self.assertTrue(path.startswith(os.path.join(import_preview.job_dir(""), "")), path)
            self.assertFalse(os.path.exists(path), f"{path} was left behind")

    def test_no_entry_outlives_a_preview_that_filled_before_reading_it(self) -> None:
        """The job directory goes at the end of the job, so this looks at it as the parse returns."""
        csv = "name,latitude,longitude\n" + "".join(f"Mill {i},{_lat(i)},{_lng(i)}\n" for i in range(_KEPT_PINS))
        upload = _zipped_all([(f"sites-{i}.csv", csv.encode()) for i in range(3)])
        with mock.patch(ENQUEUE, return_value=mock.Mock()):
            job_id = import_preview.start_import_preview(self.profile, [SimpleUploadedFile("export.zip", upload)])
        directory = import_preview.job_dir(job_id)
        self.addCleanup(import_preview.shutil.rmtree, directory, True)

        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", _KEPT_PINS),
        ):
            parsed = import_preview._read_uploads(self.profile, directory, ["export.zip"])

        self.assertEqual([entry["stem"] for entry in parsed["lists"]], ["sites-0"], "the premise failed")
        self.assertEqual([name for name in os.listdir(os.path.join(directory, "uploads")) if name != "0"], [])
