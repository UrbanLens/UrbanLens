"""An imported photo's coordinates are validated like a repositioned photo's, and a bad pair costs the photo its location, not the import."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.import_export import import_data
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import _fixtures

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


class ImportedPhotoCoordinateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.importer = baker.make(User).profile
        self.archive = Path(tempfile.mkdtemp(prefix="ul_import_coords_"))
        self.addCleanup(shutil.rmtree, self.archive, ignore_errors=True)
        self.name, data = _fixtures()["jpeg-exif-gps"]
        (self.archive / "photos").mkdir()
        (self.archive / "photos" / self.name).write_bytes(data)

    def _import(self, *rows: dict) -> import_data.ImportResult:
        metadata = [{"filename": self.name, "media_type": "photo", **row} for row in rows]
        (self.archive / "photos" / "metadata.json").write_text(json.dumps(metadata))
        result = import_data.ImportResult()
        with self.captureOnCommitCallbacks(execute=True), mock.patch(_ENQUEUE):
            import_data._import_photos(self.importer, str(self.archive), result, pin_uuid_map={}, label_uuid_map={})
        return result

    def test_valid_coordinates_are_kept(self) -> None:
        self._import({"latitude": "41.5", "longitude": "-73.25"})

        image = Image.objects.get(profile=self.importer)
        self.assertEqual((image.latitude, image.longitude), (Decimal("41.5"), Decimal("-73.25")))

    def test_a_nan_pair_imports_the_photo_without_a_location(self) -> None:
        self._import({"latitude": "NaN", "longitude": "NaN"})

        image = Image.objects.get(profile=self.importer)
        self.assertIsNone(image.latitude)
        self.assertIsNone(image.longitude)

    def test_an_out_of_range_pair_does_not_fail_the_import(self) -> None:
        result = self._import(
            {"latitude": "1000", "longitude": "5"},
            {"latitude": "10", "longitude": "-500"},
            {"latitude": "12.5", "longitude": "7.5"},
        )

        self.assertEqual(
            Image.objects.filter(profile=self.importer, latitude__isnull=True, longitude__isnull=True).count(), 2
        )
        self.assertEqual(
            Image.objects.filter(profile=self.importer, latitude=Decimal("12.5"), longitude=Decimal("7.5")).count(), 1
        )
        self.assertEqual(result.created.get("photos"), 3)

    def test_half_a_pair_is_dropped(self) -> None:
        self._import({"latitude": "41.5", "longitude": None})

        image = Image.objects.get(profile=self.importer)
        self.assertIsNone(image.latitude)
        self.assertIsNone(image.longitude)
