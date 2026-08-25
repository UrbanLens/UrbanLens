"""One-off backfill: remove the EXIF block from photos stored before it was stripped on upload.

TEMPORARY - delete this command once it has been run against production.

Uploads stop carrying EXIF from the commit that added this, but every file
already in storage still has its block, and those are the ones that have had
time to be contributed to wikis. The values are preserved on the row first
(``exif_data``) for any photo that never recorded them, so the provenance
survives even though the file no longer carries it.

Deliberately passes ``max_dimension=None, convert_webp=False`` rather than each
uploader's policy: this is a scrub, not a re-processing run, and resizing or
re-encoding somebody's existing photos is a bigger change than they asked for.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import DatabaseError
from PIL import UnidentifiedImageError

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import downscale_stored_image, extract_exif_data


class Command(BaseCommand):
    """Strip embedded EXIF from every stored photo, recording it on the row first."""

    help = "Remove the EXIF block from photos already in storage, keeping the values on Image.exif_data."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
        parser.add_argument("--limit", type=int, default=None, help="Stop after this many rows (for a first cautious pass).")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        queryset = Image.objects.exclude(image="").exclude(image__isnull=True).order_by("pk")
        total = queryset.count()
        self.stdout.write(f"Scanning {total} stored photo(s){' (limit ' + str(limit) + ')' if limit else ''}.")

        stripped = 0
        recorded = 0
        skipped = 0
        failed = 0

        for index, image in enumerate(queryset.iterator()):
            if limit is not None and index >= limit:
                break
            try:
                changed, recorded_here = self._scrub(image, dry_run=dry_run)
            except (OSError, ValueError, UnidentifiedImageError, DatabaseError) as exc:
                self.stderr.write(f"  [pk={image.pk}] {type(exc).__name__}: {exc}")
                failed += 1
                continue
            if changed:
                stripped += 1
            else:
                skipped += 1
            if recorded_here:
                recorded += 1

        verb = "Would strip" if dry_run else "Stripped"
        self.stdout.write(f"Done. {verb} {stripped}, already clean {skipped}, exif_data recorded {recorded}, failed {failed}.")

    def _scrub(self, image: Image, *, dry_run: bool) -> tuple[bool, bool]:
        """Record then remove one photo's EXIF.

        Args:
            image: The row whose stored file to scrub.
            dry_run: When True, report without writing anything.

        Returns:
            (whether the file was (or would be) rewritten, whether exif_data was recorded).

        Raises:
            OSError: The file cannot be read from or written to storage.
            ValueError: Pillow could not make sense of the file.
        """
        recorded = False
        if image.exif_data is None:
            with image.image.open("rb") as handle:
                extracted = extract_exif_data(handle)
            if extracted:
                recorded = True
                if not dry_run:
                    Image.objects.filter(pk=image.pk).update(exif_data=extracted)

        if dry_run:
            # Reading is enough to know whether there is a block to remove; the
            # rewrite itself is what we are not doing.
            with image.image.open("rb") as handle:
                return bool(extract_exif_data(handle)), recorded

        new_size = downscale_stored_image(image, max_dimension=None, convert_webp=False)
        if new_size is None:
            return False, recorded

        Image.objects.filter(pk=image.pk).update(image=image.image.name, file_size=new_size)
        return True, recorded
