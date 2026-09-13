"""One-off backfill: re-encode photos stored before every upload was re-encoded.

TEMPORARY - delete this command once it has been run against production. Run it once: each run re-encodes again.

A photo used to be stored as uploaded unless it was resized, converted or carried EXIF, so older files can still hold
XMP (which can carry GPS), IPTC, a comment or PNG text. Each photo's EXIF and embedded keywords are recorded on its row
when they never were, then the file is re-encoded under the uploader's format policy, and an existing analysis copy is
rewritten from the clean file.

Dimensions are kept. Shrinking cannot be undone, and this pass cleans files rather than applying a size cap to photos
uploaded before one existed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import DatabaseError
from PIL.Image import DecompressionBombError

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.media.images import (
    discard_superseded_file,
    downscale_stored_image,
    extract_embedded_keywords,
    extract_exif_data,
    write_image_analysis_thumbnail,
)
from urbanlens.dashboard.services.media.storage import get_stored_photo_policy
from urbanlens.dashboard.services.sandbox import allow_untrusted_parse


class Command(BaseCommand):
    """Re-encode every stored photo, recording its metadata on the row first."""

    help = "Re-encode photos already in storage so no metadata stays in the file, keeping EXIF and keywords on the row. Run once."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report how many photos would be re-encoded without reading or writing any.")
        parser.add_argument("--limit", type=int, default=None, help="Stop after this many rows (for a first cautious pass).")

    def handle(self, *args, **options):
        queryset = Image.objects.filter(media_type=MediaKind.PHOTO).exclude(image="").exclude(image__isnull=True).order_by("pk")
        if options["limit"] is not None:
            queryset = queryset[: options["limit"]]

        if options["dry_run"]:
            self.stdout.write(f"Would re-encode {queryset.count()} stored photo(s).")
            return

        reencoded = 0
        recorded = 0
        failed = 0
        # A management command has no UL_PROCESS_ROLE, so the sandbox guard would refuse the decode. These files are
        # already stored, scanned and served rather than a stranger's fresh upload; the exemption is scoped to the loop.
        with allow_untrusted_parse("strip_exif_from_stored_photos: already-stored, already-scanned files"):
            for image in queryset.iterator():
                try:
                    recorded += self._reencode(image)
                except (OSError, ValueError, EOFError, SyntaxError, DecompressionBombError, DatabaseError) as exc:
                    self.stderr.write(f"  [pk={image.pk}] {type(exc).__name__}: {exc}")
                    failed += 1
                    continue
                reencoded += 1

        self.stdout.write(f"Done. Re-encoded {reencoded}, exif_data recorded {recorded}, failed {failed}.")

    def _reencode(self, image: Image) -> bool:
        """Record one photo's metadata on its row, then re-encode its file and analysis copy.

        Args:
            image: The row whose stored file to re-encode.

        Returns:
            Whether exif_data was recorded.

        Raises:
            OSError: The file cannot be read from or written to storage.
            ValueError: Pillow could not make sense of the file.
        """
        recorded = False
        if image.exif_data is None:
            with image.image.open("rb") as handle:
                extracted = extract_exif_data(handle)
            if extracted:
                Image.objects.filter(pk=image.pk).update(exif_data=extracted)
                recorded = True
        if image.embedded_keywords is None:
            with image.image.open("rb") as handle:
                keywords = extract_embedded_keywords(handle)
            if keywords is not None:
                Image.objects.filter(pk=image.pk).update(embedded_keywords=keywords)

        _, convert_webp = get_stored_photo_policy(image)
        replacement = downscale_stored_image(image, max_dimension=None, convert_webp=convert_webp)
        if replacement is None:
            return recorded

        Image.objects.filter(pk=image.pk).update(image=image.image.name, file_size=replacement.size)
        # After the update, never before: until the row names the rewritten file, deleting the old one leaves an
        # authorized request with nothing to open.
        discard_superseded_file(image, replacement.superseded_name)

        if image.analysis_thumbnail and write_image_analysis_thumbnail(image, force=True):
            Image.objects.filter(pk=image.pk).update(analysis_thumbnail=image.analysis_thumbnail.name)
        return recorded
