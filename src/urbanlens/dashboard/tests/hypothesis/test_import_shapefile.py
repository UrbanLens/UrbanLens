"""Tests for Shapefile import: how sidecar parts are grouped into bundles, and what the preview reads from one."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.import_formats.shapefile import ShapefileSpool, is_shapefile_part

_SAMPLE_SHAPEFILE_DIR = Path(__file__).resolve().parents[5] / "sample_data" / "sample_shapefile"
_SAMPLE_PARTS = ("shp", "dbf", "shx", "prj", "cpg")


class ShapefileSpoolTests(SimpleTestCase):
    """``ShapefileSpool`` groups same-stem sidecar parts and yields only complete bundles."""

    def _bundles(self, filenames: list[str]) -> list[tuple[str, set[str]]]:
        with tempfile.TemporaryDirectory() as directory:
            spool = ShapefileSpool(directory)
            for filename in filenames:
                spool.add(filename, io.BytesIO(filename.encode()))
            return [
                (stem, {path.suffix.lstrip(".") for path in shp_path.parent.glob(f"{shp_path.stem}.*")})
                for stem, shp_path in spool.bundles()
            ]

    def test_a_complete_bundle_is_grouped(self) -> None:
        self.assertEqual(self._bundles(["sites.shp", "sites.dbf", "sites.shx"]), [("sites", {"shp", "dbf", "shx"})])

    def test_a_bundle_without_its_dbf_is_dropped(self) -> None:
        self.assertEqual(self._bundles(["sites.shp", "sites.shx"]), [])

    def test_bundles_are_grouped_independently_in_arrival_order(self) -> None:
        bundles = self._bundles(["b.shp", "a.shp", "a.dbf", "b.dbf"])

        self.assertEqual([stem for stem, _ in bundles], ["b", "a"])

    def test_stems_match_case_insensitively(self) -> None:
        self.assertEqual(self._bundles(["Sites.SHP", "sites.dbf"]), [("sites", {"shp", "dbf"})])

    def test_only_sidecar_extensions_are_parts(self) -> None:
        for filename in ("a.shp", "a.DBF", "a.shx", "a.prj", "a.cpg"):
            with self.subTest(filename=filename):
                self.assertTrue(is_shapefile_part(filename))
        for filename in ("places.kml", "notes.csv", "shp", "a.shp.zip"):
            with self.subTest(filename=filename):
                self.assertFalse(is_shapefile_part(filename))


class ShapefilePreviewTests(SimpleTestCase):
    """``parse_for_preview`` reads a real Shapefile bundle into pins."""

    def _preview(self, files: list[tuple[str, bytes]]):
        return GoogleMapsGateway(api_key="").parse_for_preview(files, Profile())

    def test_real_world_sample_bundle(self) -> None:
        files = [
            (f"sample_airports.{ext}", (_SAMPLE_SHAPEFILE_DIR / f"sample_airports.{ext}").read_bytes())
            for ext in _SAMPLE_PARTS
        ]

        parse = self._preview(files)

        self.assertEqual(parse.failed_formats, [])
        self.assertEqual([entry["stem"] for entry in parse.lists], ["sample_airports"])
        pins = parse.lists[0]["pins"]
        # sample_airports.shp has 5 real major airports (JFK, ORD, LAX, LHR, NRT).
        self.assertEqual(len(pins), 5)
        self.assertTrue(any("Kennedy" in pin["name"] for pin in pins))
        for pin in pins:
            self.assertIsInstance(pin["lat"], float)
            self.assertIsInstance(pin["lng"], float)

    def test_an_unreadable_bundle_is_reported_as_a_failed_shapefile(self) -> None:
        parse = self._preview([("broken.shp", b"not a real shp"), ("broken.dbf", b"not a real dbf")])

        self.assertEqual(parse.lists, [])
        self.assertEqual(parse.failed_formats, ["shapefile"])
