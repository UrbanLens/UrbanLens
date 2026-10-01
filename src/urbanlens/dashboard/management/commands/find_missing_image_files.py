"""List ``Image`` rows whose file columns name a file that is not in storage, and optionally regenerate derived copies.

The reverse of P14, which is files no row names. A row naming a missing file is served as a broken image for as long
as it exists. ``--repair`` clears a photo's missing thumbnail, marker or analysis copy and queues its regeneration from
the stored original; a row whose original is gone has nothing to regenerate from and is only reported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.media.held_upload import STORAGE_ERRORS
from urbanlens.dashboard.services.media.images import THUMBNAIL_BACKFILL_BATCH

if TYPE_CHECKING:
    from argparse import ArgumentParser

FILE_COLUMNS = ("image", "thumbnail", "marker_thumbnail", "analysis_thumbnail")


class Command(BaseCommand):
    """Report rows naming missing files; ``--repair`` regenerates a photo's missing derived copies."""

    help = "List Image rows whose image/thumbnail/marker_thumbnail/analysis_thumbnail names a file missing from storage. --repair queues regeneration of a photo's missing derived copies."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--repair", action="store_true", help="Clear each missing derived copy on a processed photo whose original exists, and queue its regeneration.")

    def handle(self, *args: Any, **options: Any) -> None:
        missing: dict[str, list[int]] = {column: [] for column in FILE_COLUMNS}
        repairable: set[int] = set()
        unchecked = 0
        for image in Image.objects.only("pk", "media_type", "pending_scan", *FILE_COLUMNS).order_by("pk").iterator(chunk_size=500):
            gone = []
            for column in FILE_COLUMNS:
                name = getattr(image, column).name
                if not name:
                    continue
                try:
                    exists = getattr(image, column).storage.exists(name)
                except STORAGE_ERRORS as exc:
                    self.stderr.write(f"pk={image.pk} {column} {name}: {type(exc).__name__}: {exc}")
                    unchecked += 1
                    continue
                if not exists:
                    gone.append(column)
                    missing[column].append(image.pk)
                    self.stdout.write(f"pk={image.pk} {column} {name}")
            if gone and "image" not in gone and image.media_type == MediaKind.PHOTO and not image.pending_scan:
                repairable.add(image.pk)

        for column, pks in missing.items():
            self.stdout.write(f"{column}: {len(pks)} missing")
        if unchecked:
            self.stdout.write(f"{unchecked} file(s) could not be checked.")
        if options["repair"]:
            self._repair(missing, repairable)

    def _repair(self, missing: dict[str, list[int]], repairable: set[int]) -> None:
        """Clear each repairable row's missing derived columns and queue the task that writes them.

        Args:
            missing: Row pks per column whose file is missing.
            repairable: Processed photos whose original is present.
        """
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import generate_image_analysis_thumbnails, generate_image_marker_thumbnails, generate_image_thumbnails

        tasks = {
            "thumbnail": generate_image_thumbnails,
            "marker_thumbnail": generate_image_marker_thumbnails,
            "analysis_thumbnail": generate_image_analysis_thumbnails,
        }
        for column, task in tasks.items():
            pks = [pk for pk in missing[column] if pk in repairable]
            if not pks:
                continue
            Image.objects.filter(pk__in=pks).update(**{column: ""})
            for start in range(0, len(pks), THUMBNAIL_BACKFILL_BATCH):
                safely_enqueue_task(task, pks[start : start + THUMBNAIL_BACKFILL_BATCH])
            self.stdout.write(f"{column}: cleared and queued {len(pks)}")
        unrepairable = len(set(missing["image"]))
        if unrepairable:
            self.stdout.write(f"{unrepairable} row(s) name an original that is gone; nothing to regenerate from.")
