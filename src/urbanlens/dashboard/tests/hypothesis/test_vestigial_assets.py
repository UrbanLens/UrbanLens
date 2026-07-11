"""Tests for vestigial media artifact cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.export import EXPORT_TTL_SECONDS
from urbanlens.dashboard.services.vestigial_assets import cleanup_vestigial_assets
from urbanlens.dashboard.tasks import cleanup_vestigial_assets_task


def _touch_dir(path: Path, when: datetime) -> None:
    path.mkdir(parents=True, exist_ok=True)
    timestamp = when.timestamp()
    os.utime(path, (timestamp, timestamp))


class CleanupVestigialAssetsTests(TestCase):
    """cleanup_vestigial_assets removes stale managed artifact directories only."""

    def test_deletes_expired_import_and_export_directories(self) -> None:
        now = datetime(2026, 1, 1, 12, tzinfo=UTC)
        expired = now - timedelta(seconds=EXPORT_TTL_SECONDS + 1)

        with TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            stale_export = media_root / "exports" / "stale-export"
            stale_import = media_root / "imports" / "stale-import"
            _touch_dir(stale_export, expired)
            _touch_dir(stale_import, expired)

            with mock.patch("urbanlens.dashboard.services.vestigial_assets.django_settings.MEDIA_ROOT", str(media_root)):
                result = cleanup_vestigial_assets(now=now)

            self.assertEqual(result.deleted, 2)
            self.assertFalse(stale_export.exists())
            self.assertFalse(stale_import.exists())

    def test_keeps_recent_directories_and_non_directory_files(self) -> None:
        now = datetime(2026, 1, 1, 12, tzinfo=UTC)

        with TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            recent_export = media_root / "exports" / "recent-export"
            _touch_dir(recent_export, now)
            marker_file = media_root / "exports" / "README.txt"
            marker_file.write_text("not a job directory", encoding="utf-8")

            with mock.patch("urbanlens.dashboard.services.vestigial_assets.django_settings.MEDIA_ROOT", str(media_root)):
                result = cleanup_vestigial_assets(now=now)

            self.assertEqual(result.deleted, 0)
            self.assertEqual(result.skipped, 2)
            self.assertTrue(recent_export.exists())
            self.assertTrue(marker_file.exists())

    def test_creates_missing_managed_directories_without_warning(self) -> None:
        now = datetime(2026, 1, 1, 12, tzinfo=UTC)

        with TemporaryDirectory() as tmp:
            media_root = Path(tmp)

            with mock.patch("urbanlens.dashboard.services.vestigial_assets.django_settings.MEDIA_ROOT", str(media_root)):
                with self.assertNoLogs("urbanlens.dashboard.services.vestigial_assets", level="WARNING"):
                    result = cleanup_vestigial_assets(now=now)

            self.assertEqual(result.total, 0)
            self.assertTrue((media_root / "exports").is_dir())
            self.assertTrue((media_root / "imports").is_dir())


class CleanupVestigialAssetsTaskTests(TestCase):
    """The scheduled Celery task returns a serializable cleanup summary."""

    def test_returns_result_payload(self) -> None:
        fake_result = mock.Mock()
        fake_result.as_dict.return_value = {"scanned": 1337, "deleted": 3, "skipped": 1334, "errors": 0}
        fake_result.total = 1337

        with mock.patch("urbanlens.dashboard.services.vestigial_assets.cleanup_vestigial_assets", return_value=fake_result):
            self.assertDictEqual(
                cleanup_vestigial_assets_task(),
                {"scanned": 1337, "deleted": 3, "skipped": 1334, "errors": 0},
            )
