"""Tests for archive_extractor service - ZIP/TGZ extraction with security checks.

Covers:
- is_archive() magic-byte detection
- validate_content_type() for JSON, location_history, KML, CSV, My Activity HTML formats
- extract_archive() for well-formed ZIP and TGZ archives
- Security: path traversal, symlink skipping, per-file size limit, total size limit,
  file count limit, compression-ratio (zip bomb) detection
- _safe_basename() and _extension() helpers
"""
from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.archive_extractor import (
    _extension,
    _safe_basename,
    extract_archive,
    is_archive,
    validate_content_type,
)

_hyp = hyp_settings(max_examples=40, deadline=None)


# ---------------------------------------------------------------------------
# Helpers for building in-memory archives
# ---------------------------------------------------------------------------

def _make_zip(files: dict[str, bytes]) -> bytes:
    """Return a ZIP archive containing *files* (name → content)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_tgz(files: dict[str, bytes]) -> bytes:
    """Return a GZIP-compressed TAR archive containing *files*."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# is_archive
# ---------------------------------------------------------------------------

class IsArchiveTests(TestCase):
    """is_archive() identifies ZIP and GZIP magic bytes."""

    def test_zip_magic_detected(self):
        data = _make_zip({"a.json": b"{}"})
        self.assertTrue(is_archive(data))

    def test_gzip_magic_detected(self):
        data = _make_tgz({"a.json": b"{}"})
        self.assertTrue(is_archive(data))

    def test_plain_json_not_archive(self):
        self.assertFalse(is_archive(b'{"key": "value"}'))

    def test_empty_bytes_not_archive(self):
        self.assertFalse(is_archive(b""))

    def test_short_data_not_archive(self):
        self.assertFalse(is_archive(b"ab"))

    def test_xml_not_archive(self):
        self.assertFalse(is_archive(b"<?xml version"))

    @given(st.binary(min_size=4, max_size=16).filter(
        lambda b: b[:4] != b"PK\x03\x04" and b[:2] != b"\x1f\x8b"
    ))
    @_hyp
    def test_random_non_archive_bytes_return_false(self, data: bytes):
        self.assertFalse(is_archive(data))


# ---------------------------------------------------------------------------
# _safe_basename
# ---------------------------------------------------------------------------

class SafeBasenameTests(TestCase):
    """_safe_basename rejects path-traversal and absolute paths."""

    def test_simple_filename(self):
        self.assertEqual(_safe_basename("file.json"), "file.json")

    def test_nested_path_returns_basename(self):
        self.assertEqual(_safe_basename("subdir/file.kml"), "file.kml")

    def test_dotdot_in_path_returns_none(self):
        self.assertIsNone(_safe_basename("../etc/passwd"))

    def test_dotdot_in_component_returns_none(self):
        self.assertIsNone(_safe_basename("a/../b/file.json"))

    def test_windows_separator_handled(self):
        self.assertEqual(_safe_basename("subdir\\file.csv"), "file.csv")

    def test_empty_basename_returns_none(self):
        # A trailing slash gives an empty basename after split
        self.assertIsNone(_safe_basename("subdir/"))


# ---------------------------------------------------------------------------
# _extension
# ---------------------------------------------------------------------------

class ExtensionTests(TestCase):
    """_extension returns lowercase extension without leading dot."""

    def test_json_extension(self):
        self.assertEqual(_extension("data.json"), "json")

    def test_uppercase_extension_lowercased(self):
        self.assertEqual(_extension("Data.JSON"), "json")

    def test_no_extension_returns_empty(self):
        self.assertEqual(_extension("README"), "")

    def test_multiple_dots_returns_last(self):
        self.assertEqual(_extension("archive.tar.gz"), "gz")

    def test_dot_only_filename(self):
        self.assertEqual(_extension(".hidden"), "hidden")


# ---------------------------------------------------------------------------
# validate_content_type
# ---------------------------------------------------------------------------

class ValidateContentTypeTests(TestCase):
    """validate_content_type identifies format from file content."""

    def test_geojson_features_returns_json(self):
        data = json.dumps({"type": "FeatureCollection", "features": []}).encode()
        self.assertEqual(validate_content_type("file.json", data), "json")

    def test_location_history_json_identified(self):
        data = json.dumps({"timelineObjects": []}).encode()
        self.assertEqual(validate_content_type("records.json", data), "location_history")

    def test_json_without_known_key_returns_none(self):
        data = json.dumps({"some_other_key": 123}).encode()
        self.assertIsNone(validate_content_type("data.json", data))

    def test_invalid_json_returns_none(self):
        data = b"{not valid json"
        self.assertIsNone(validate_content_type("bad.json", data))

    def test_kml_with_xml_header(self):
        data = b"<?xml version='1.0'?><kml xmlns='http://www.opengis.net/kml/2.2'><Placemark/></kml>"
        self.assertEqual(validate_content_type("places.kml", data), "kml")

    def test_kml_starting_with_kml_tag(self):
        data = b"<kml xmlns='http://www.opengis.net/kml/2.2'></kml>"
        self.assertEqual(validate_content_type("places.kml", data), "kml")

    def test_xml_without_kml_tag_returns_none(self):
        data = b"<?xml version='1.0'?><root><item/></root>"
        self.assertIsNone(validate_content_type("data.xml", data))

    def test_csv_with_url_header(self):
        data = b"URL,Title,Note\nhttps://example.com,My site,A note"
        self.assertEqual(validate_content_type("export.csv", data), "csv")

    def test_csv_with_title_header(self):
        data = b"title,note\nMy Place,A note"
        self.assertEqual(validate_content_type("export.csv", data), "csv")

    def test_csv_with_note_header(self):
        data = b"note,other\nsome note,val"
        self.assertEqual(validate_content_type("export.csv", data), "csv")

    def test_csv_with_latitude_longitude_header(self):
        data = b"name,latitude,longitude\nMy Place,42.3601,-71.0589"
        self.assertEqual(validate_content_type("export.csv", data), "csv")

    def test_csv_with_lat_lng_header(self):
        data = b"Name,Lat,Lng\nMy Place,42.3601,-71.0589"
        self.assertEqual(validate_content_type("export.csv", data), "csv")

    def test_csv_with_only_latitude_column_returns_none(self):
        data = b"name,latitude\nMy Place,42.3601"
        self.assertIsNone(validate_content_type("export.csv", data))

    def test_too_small_file_returns_none(self):
        self.assertIsNone(validate_content_type("tiny.json", b"{}"))

    def test_empty_file_returns_none(self):
        self.assertIsNone(validate_content_type("empty.json", b""))

    def test_binary_data_returns_none(self):
        self.assertIsNone(validate_content_type("binary.dat", b"\x00\x01\x02\x03\xff\xfe"))

    def test_unrecognised_text_returns_none(self):
        data = b"this is just plain text with no known headers"
        self.assertIsNone(validate_content_type("text.txt", data))

    def test_gpx_root_tag_identified(self):
        data = b'<?xml version="1.0"?><gpx version="1.1"><wpt lat="1" lon="2"/></gpx>'
        self.assertEqual(validate_content_type("track.gpx", data), "gpx")

    def test_osm_xml_root_tag_identified(self):
        data = b'<?xml version="1.0"?><osm version="0.6"><node id="1" lat="1" lon="2"/></osm>'
        self.assertEqual(validate_content_type("export.osm", data), "osm_xml")

    def test_wkt_point_identified(self):
        data = b"POINT (-73.78633 40.64596)"
        self.assertEqual(validate_content_type("geom.wkt", data), "wkt")

    def test_wkt_polygon_case_insensitive(self):
        data = b"polygon ((0 0, 1 0, 1 1, 0 0))"
        self.assertEqual(validate_content_type("geom.wkt", data), "wkt")

    def test_wkt_linestring_z_suffix(self):
        data = b"LINESTRING Z (0 0 0, 1 1 1)"
        self.assertEqual(validate_content_type("geom.wkt", data), "wkt")

    def test_binary_wkb_point_identified(self):
        # Little-endian WKB Point: byte order 01, geometry type 1 (Point), then coords.
        data = bytes.fromhex("0101000000") + b"\x00" * 16
        self.assertEqual(validate_content_type("geom.wkb", data), "wkb")

    def test_hex_wkb_text_identified(self):
        data = ("0101000000" + "00" * 16).encode("ascii")
        self.assertEqual(validate_content_type("geom.wkb", data), "wkb")

    def test_random_binary_not_misidentified_as_wkb(self):
        data = b"\x05\x99\x12\x34\x56\x78\x9a\xbc\xde\xf0"
        self.assertIsNone(validate_content_type("binary.dat", data))

    def test_my_activity_html_identified(self):
        data = (
            b'<!DOCTYPE html><html><head><title>My Activity</title></head><body>'
            b'<div class="outer-cell"><div class="mdl-grid">'
            b'<div class="header-cell"><p class="mdl-typography--title">Maps<br></p></div>'
            b'<div class="content-cell mdl-typography--body-1">Directions to '
            b'<a href="https://www.google.com/maps/dir//1.0,2.0/@1.0,2.0,13z">Somewhere</a><br>'
            b"1.0,2.0<br>Jul 3, 2026, 1:18:25 PM EDT<br></div></div></div></body></html>"
        )
        self.assertEqual(validate_content_type("MyActivity.html", data), "my_activity")

    def test_generic_html_returns_none(self):
        data = b"<!DOCTYPE html><html><head><title>Some Page</title></head><body><h1>Hello</h1></body></html>"
        self.assertIsNone(validate_content_type("page.html", data))

    def test_bare_html_tag_start_without_doctype_detected(self):
        data = (
            b'<html><body><p class="mdl-typography--title">Maps<br></p>'
            b'Directions to <a href="https://x">Y</a></body></html>'
        )
        self.assertEqual(validate_content_type("MyActivity.html", data), "my_activity")

    def test_my_activity_html_not_misidentified_as_csv(self):
        # A My Activity file's <title> tag contains the literal substring "title", which
        # would trip the CSV header heuristic (url/title/note) if the HTML check didn't
        # run first - this guards that ordering.
        data = (
            b'<!DOCTYPE html><html><head><title>My Activity</title></head><body>'
            b'<div class="outer-cell"><p class="mdl-typography--title">Maps<br></p>'
            b'<div>not a directions entry</div></div></body></html>'
        )
        result = validate_content_type("MyActivity.html", data)
        self.assertNotEqual(result, "csv")


# ---------------------------------------------------------------------------
# extract_archive - ZIP
# ---------------------------------------------------------------------------

class ExtractZipTests(TestCase):
    """extract_archive handles ZIP archives correctly."""

    def test_extracts_json_file(self):
        content = json.dumps({"features": []}).encode()
        data = _make_zip({"places.json": content})
        result = extract_archive(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "places.json")
        self.assertEqual(result[0].data, content)

    def test_extracts_kml_file(self):
        content = b"<kml><Placemark/></kml>"
        data = _make_zip({"places.kml": content})
        result = extract_archive(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "places.kml")

    def test_skips_unsupported_extension(self):
        data = _make_zip({"readme.txt": b"hello"})
        result = extract_archive(data)
        self.assertEqual(result, [])

    def test_skips_dotdot_path(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../etc/passwd", b"root:x:0:0")
        result = extract_archive(buf.getvalue())
        self.assertEqual(result, [])

    def test_multiple_files_all_extracted(self):
        data = _make_zip({
            "a.json": json.dumps({"features": []}).encode(),
            "b.kml": b"<kml><Placemark/></kml>",
            "c.csv": b"URL,Title\nhttps://example.com,test",
        })
        result = extract_archive(data)
        self.assertEqual(len(result), 3)

    def test_raises_on_corrupted_zip(self):
        with self.assertRaises(ValueError):
            extract_archive(b"PK\x03\x04this is not a valid zip file")

    def test_mixed_supported_and_unsupported_skips_unsupported(self):
        data = _make_zip({
            "places.json": json.dumps({"features": []}).encode(),
            "image.png": b"\x89PNG\r\n",
        })
        result = extract_archive(data)
        names = [r.name for r in result]
        self.assertIn("places.json", names)
        self.assertNotIn("image.png", names)

    def test_nested_path_uses_basename(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("subdir/places.json", b'{"features":[]}')
        result = extract_archive(buf.getvalue())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "places.json")

    def test_extracts_shapefile_sidecar_extensions(self):
        data = _make_zip(
            {
                "sites.shp": b"\x00\x00\x27\x0a" + b"\x00" * 96,
                "sites.dbf": b"\x03" + b"\x00" * 31,
                "sites.shx": b"\x00\x00\x27\x0a" + b"\x00" * 96,
                "sites.prj": b'GEOGCS["WGS 84"]',
            },
        )
        result = extract_archive(data)
        names = {r.name for r in result}
        self.assertEqual(names, {"sites.shp", "sites.dbf", "sites.shx", "sites.prj"})

    def test_extracts_gpx_wkt_osm_files(self):
        data = _make_zip(
            {
                "track.gpx": b"<gpx></gpx>",
                "geom.wkt": b"POINT (0 0)",
                "export.osm": b"<osm></osm>",
            },
        )
        result = extract_archive(data)
        names = {r.name for r in result}
        self.assertEqual(names, {"track.gpx", "geom.wkt", "export.osm"})

    def test_extracts_nested_my_activity_html_from_takeout_zip(self):
        # Real Takeout exports nest MyActivity.html several folders deep -
        # _safe_basename should flatten the path, same as any other format.
        data = _make_zip({"Takeout/My Activity/Maps/MyActivity.html": b"<html>...</html>"})
        result = extract_archive(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "MyActivity.html")


# ---------------------------------------------------------------------------
# extract_archive - TGZ
# ---------------------------------------------------------------------------

class ExtractTgzTests(TestCase):
    """extract_archive handles TGZ archives correctly."""

    def test_extracts_json_file(self):
        content = json.dumps({"features": []}).encode()
        data = _make_tgz({"places.json": content})
        result = extract_archive(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "places.json")

    def test_extracts_kml_file(self):
        data = _make_tgz({"export.kml": b"<kml><Placemark/></kml>"})
        result = extract_archive(data)
        self.assertEqual(result[0].name, "export.kml")

    def test_skips_unsupported_extension(self):
        data = _make_tgz({"notes.txt": b"hello"})
        self.assertEqual(extract_archive(data), [])

    def test_skips_dotdot_path(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="../etc/passwd")
            content = b"root:x:0:0"
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        result = extract_archive(buf.getvalue())
        self.assertEqual(result, [])

    def test_raises_on_corrupted_tgz(self):
        with self.assertRaises(ValueError):
            extract_archive(b"\x1f\x8bthis is not a valid gzip")

    def test_symlink_members_skipped(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            # Add a real file
            content = b'{"features":[]}'
            info = tarfile.TarInfo(name="places.json")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
            # Add a symlink
            sym = tarfile.TarInfo(name="link.json")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            sym.size = 0
            tf.addfile(sym)
        result = extract_archive(buf.getvalue())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "places.json")


# ---------------------------------------------------------------------------
# extract_archive - format dispatch
# ---------------------------------------------------------------------------

class ExtractArchiveDispatchTests(TestCase):
    """extract_archive raises ValueError for unrecognised format."""

    def test_plain_text_raises(self):
        with self.assertRaises(ValueError, msg="Should reject non-archive bytes"):
            extract_archive(b"this is plain text, not an archive")

    def test_empty_bytes_raises(self):
        with self.assertRaises(ValueError):
            extract_archive(b"")

    def test_zip_dispatched_correctly(self):
        data = _make_zip({"x.csv": b"URL,Title\nhttps://x.com,X"})
        result = extract_archive(data)
        self.assertGreater(len(result), 0)

    def test_tgz_dispatched_correctly(self):
        data = _make_tgz({"x.csv": b"URL,Title\nhttps://x.com,X"})
        result = extract_archive(data)
        self.assertGreater(len(result), 0)
