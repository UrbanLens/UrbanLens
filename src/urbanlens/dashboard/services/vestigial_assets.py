"""Cleanup helpers for vestigial user artifact directories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import shutil

from django.conf import settings as django_settings

from urbanlens.dashboard.services.export import EXPORT_TTL_SECONDS
from urbanlens.dashboard.services.import_data import IMPORT_TTL_SECONDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VestigialAssetCleanupResult:
    """Summary of a vestigial asset cleanup pass."""

    scanned: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return a JSON-serializable representation for Celery result backends."""
        return {
            "scanned": self.scanned,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "errors": self.errors,
        }
        
    @property
    def total(self) -> int:
        """Return the total number of artifacts scanned."""
        return self.scanned + self.deleted + self.skipped + self.errors


_MANAGED_ARTIFACT_DIRS = {
    "exports": EXPORT_TTL_SECONDS,
    "imports": IMPORT_TTL_SECONDS,
}


def cleanup_vestigial_assets(*, now: datetime | None = None) -> VestigialAssetCleanupResult:
    """Delete stale managed artifacts left behind after one-off cleanup failures.

    Export and import jobs schedule per-job deletion after their TTL expires. This
    sweep is a safety net for cases where that delayed Celery task could not be
    enqueued or failed after the artifact was already eligible for deletion.
    """
    reference_time = now or datetime.now(UTC)
    media_root = Path(django_settings.MEDIA_ROOT).resolve()
    scanned = deleted = skipped = errors = 0

    for dirname, ttl_seconds in _MANAGED_ARTIFACT_DIRS.items():
        root = (media_root / dirname).resolve()
        if not _is_within(root, media_root) or not root.is_dir():
            logger.warning("Vestigial %s artifact directory %s is outside the media root or not a directory", dirname, root)
            continue

        cutoff = reference_time - timedelta(seconds=ttl_seconds)
        for artifact in root.iterdir():
            if not artifact.is_dir():
                skipped += 1
                continue
            scanned += 1
            try:
                artifact_mtime = datetime.fromtimestamp(artifact.stat().st_mtime, tz=UTC)
                if artifact_mtime > cutoff:
                    skipped += 1
                    continue
                shutil.rmtree(artifact)
                deleted += 1
                logger.info("Deleted vestigial %s artifact directory %s", dirname, artifact)
            except OSError:
                errors += 1
                logger.exception("Unable to delete vestigial %s artifact directory %s", dirname, artifact)

    return VestigialAssetCleanupResult(scanned=scanned, deleted=deleted, skipped=skipped, errors=errors)


def _is_within(path: Path, parent: Path) -> bool:
    """Return True when ``path`` is inside ``parent`` or equal to it."""
    return path == parent or parent in path.parents
