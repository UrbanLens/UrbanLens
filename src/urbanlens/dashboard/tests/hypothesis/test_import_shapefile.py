"""Tests for services.import_formats.shapefile - Shapefile bundle grouping and import.

A Shapefile is the odd one out among the supported formats: it's always a set of
same-stem sidecar files rather than a single file, so ``extract_shapefile_bundles()``
must correctly group parts by stem *before* ``shapefile_to_dict()`` ever runs -
that grouping step is the main regression risk covered here.
"""
from __future__ import annotations

from pathlib import Path

import pyogrio.errors

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_formats.shapefile import (
    ShapefileBundle,
    extract_shapefile_bundles,
    shapefile_to_dict,
)

_SAMPLE_SHAPEFILE_DIR = Path(__file__).resolve().parents[5] / "sample_data" / "sample_shapefile"


class ExtractShapefileBundlesTests(SimpleTestCase):
    """extract_shapefile_bundles() groups same-stem sidecar files."""

    def test_complete_bundle_grouped(self):
        files = [
            ("sites.shp", b"shp-bytes"),
            ("sites.dbf", b"dbf-bytes"),
            ("sites.shx", b"shx-bytes"),
        ]

        bundles, remaining = extract_shapefile_bundles(files)

        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].stem, "sites")
        self.assertEqual(bundles[0].parts, {"shp": b"shp-bytes", "dbf": b"dbf-bytes", "shx": b"shx-bytes"})
        self.assertEqual(remaining, [])

    def test_incomplete_bundle_dropped(self):
        # Missing the required .dbf part.
        files = [("sites.shp", b"shp-bytes")]

        bundles, remaining = extract_shapefile_bundles(files)

        self.assertEqual(bundles, [])
        self.assertEqual(remaining, [])

    def test_non_shapefile_files_pass_through_untouched(self):
        files = [("places.kml", b"<kml/>"), ("notes.csv", b"a,b")]

        bundles, remaining = extract_shapefile_bundles(files)

        self.assertEqual(bundles, [])
        self.assertEqual(remaining, files)

    def test_multiple_bundles_grouped_independently(self):
        files = [
            ("a.shp", b"a-shp"), ("a.dbf", b"a-dbf"),
            ("b.shp", b"b-shp"), ("b.dbf", b"b-dbf"),
        ]

        bundles, _ = extract_shapefile_bundles(files)

        stems = {b.stem for b in bundles}
        self.assertEqual(stems, {"a", "b"})

    def test_stem_matching_is_case_insensitive(self):
        files = [("Sites.SHP", b"shp-bytes"), ("sites.dbf", b"dbf-bytes")]

        bundles, _ = extract_shapefile_bundles(files)

        self.assertEqual(len(bundles), 1)
        self.assertEqual(set(bundles[0].parts), {"shp", "dbf"})

    def test_mixed_shapefile_and_other_files(self):
        files = [
            ("sites.shp", b"shp-bytes"),
            ("sites.dbf", b"dbf-bytes"),
            ("places.kml", b"<kml/>"),
        ]

        bundles, remaining = extract_shapefile_bundles(files)

        self.assertEqual(len(bundles), 1)
        self.assertEqual(remaining, [("places.kml", b"<kml/>")])


class ShapefileToDictTests(SimpleTestCase):
    """shapefile_to_dict() converts a real Shapefile bundle into pins."""

    def setUp(self):
        self.profile = object()

    def _load_bundle(self) -> ShapefileBundle:
        bundle = ShapefileBundle(stem="sample_airports")
        for ext in ("shp", "dbf", "shx", "prj", "cpg"):
            bundle.parts[ext] = (_SAMPLE_SHAPEFILE_DIR / f"sample_airports.{ext}").read_bytes()
        return bundle

    def test_real_world_sample_bundle(self):
        bundle = self._load_bundle()

        pins = shapefile_to_dict(bundle, self.profile)

        # sample_airports.shp has 5 real major airports (JFK, ORD, LAX, LHR, NRT).
        self.assertEqual(len(pins), 5)
        names = {p["name"] for p in pins}
        self.assertTrue(any("Kennedy" in n for n in names))
        for pin in pins:
            self.assertIs(pin["profile"], self.profile)
            self.assertIsInstance(pin["latitude"], float)
            self.assertIsInstance(pin["longitude"], float)

    def test_missing_shp_extension_raises(self):
        bundle = ShapefileBundle(stem="broken")
        bundle.parts["dbf"] = b"not a real dbf"

        with self.assertRaises((OSError, ValueError, pyogrio.errors.DataSourceError)):
            shapefile_to_dict(bundle, self.profile)
