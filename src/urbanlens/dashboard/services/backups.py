"""Backup settings and statistics helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from urbanlens.UrbanLens.settings.app import settings as app_settings


@dataclass(frozen=True, slots=True)
class BackupStats:
    enabled: bool
    frequency_hours: int
    retention: int
    backup_dir: Path
    count: int
    latest_backup: datetime | None
    total_size_mb: float


def backup_files(backup_dir: Path | None = None) -> list[Path]:
    root = Path(backup_dir or app_settings.backups_dir)
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)


def collect_backup_stats(site_settings=None) -> BackupStats:
    if site_settings is None:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        site_settings = SiteSettings.get_current()
    files = backup_files()
    latest = None
    if files:
        latest = datetime.fromtimestamp(files[0].stat().st_mtime, tz=timezone.utc)
    total_size = sum(p.stat().st_size for p in files if p.exists())
    return BackupStats(
        enabled=site_settings.backup_enabled,
        frequency_hours=site_settings.backup_frequency_hours,
        retention=site_settings.backup_retention,
        backup_dir=Path(app_settings.backups_dir),
        count=len(files),
        latest_backup=latest,
        total_size_mb=round(total_size / 1_048_576, 1),
    )


def scheduled_backup_due(site_settings=None, *, now: datetime | None = None) -> bool:
    if site_settings is None:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        site_settings = SiteSettings.get_current()
    if not site_settings.backup_enabled:
        return False
    files = backup_files()
    if not files:
        return True
    current = now or datetime.now(timezone.utc)
    latest = datetime.fromtimestamp(files[0].stat().st_mtime, tz=timezone.utc)
    elapsed_hours = (current - latest).total_seconds() / 3600
    return elapsed_hours >= site_settings.backup_frequency_hours
