"""One-off backfill: re-encode photos, comment images, label icons and avatars stored before uploads of them were.

TEMPORARY - delete this command once it has been run against production. Run it once: each run re-encodes again.

A photo used to be stored as uploaded unless it was resized, converted or carried EXIF, so older files can still hold
XMP (which can carry GPS), IPTC, a comment or PNG text. Each photo's EXIF and embedded keywords are recorded on its row
when they never were, then the file is re-encoded under the uploader's format policy, and an existing analysis copy is
rewritten from the clean file.

Comment and trip comment images, label icons and avatars were always stored as uploaded (P119). They go through the
same re-encode as new uploads of them; an undecodable one is removed, and a published comment keeps its text.

Photo and comment image dimensions are kept. Shrinking cannot be undone, and this pass cleans files rather than
applying a size cap to photos uploaded before one existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
from urbanlens.dashboard.services.visits.visits import visit_logging_allowed

if TYPE_CHECKING:
    from collections.abc import Callable


class Command(BaseCommand):
    """Re-encode every stored photo, recording its metadata on the row first."""

    help = "Re-encode photos, comment images, label icons and avatars already in storage so no metadata stays in the file, keeping photo EXIF and keywords on the row. Run once."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report how many photos would be re-encoded without reading or writing any.")
        parser.add_argument("--limit", type=int, default=None, help="Stop after this many rows (for a first cautious pass).")

    def handle(self, *args, **options):
        # A pending row is the upload task's to rewrite; two writers can leave the row naming a deleted file.
        queryset = Image.objects.filter(media_type=MediaKind.PHOTO, pending_scan=False).exclude(image="").exclude(image__isnull=True).select_related("profile").order_by("pk")
        if options["limit"] is not None:
            queryset = queryset[: options["limit"]]

        if options["dry_run"]:
            self.stdout.write(f"Would re-encode {queryset.count()} stored photo(s), then every published comment image, label icon and uploaded avatar.")
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
            rewritten, other_failed = self._reencode_other_images()

        self.stdout.write(f"Done. Re-encoded {reencoded}, exif_data recorded {recorded}, failed {failed}.")
        self.stdout.write(f"Comment images, label icons and avatars: rewritten or removed {rewritten}, failed {other_failed}.")

    def _reencode_other_images(self) -> tuple[int, int]:
        """Re-encode every published comment image, label icon and uploaded avatar.

        Returns:
            How many were rewritten or removed, and how many failed on storage or the database.
        """
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.labels.model import Label
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.trips.model import TripComment
        from urbanlens.dashboard.services.labels.icons import resize_stored_icon
        from urbanlens.dashboard.services.media.storage import get_downscale_policy
        from urbanlens.dashboard.services.media.stored_field import Reencoded, clear_stored_field, reencode_stored_field
        from urbanlens.dashboard.services.profile.avatar import reencode_stored_avatar

        rewritten = 0
        failed = 0

        def attempt(label: str, pk: int, rewrite: Callable[[], bool]) -> None:
            nonlocal rewritten, failed
            try:
                rewritten += rewrite()
            except (OSError, DatabaseError) as exc:
                self.stderr.write(f"  [{label} pk={pk}] {type(exc).__name__}: {exc}")
                failed += 1

        for model, owner_field in ((Comment, "profile"), (TripComment, "author")):
            # A pending comment is its scan task's to re-encode.
            comments = model.objects.filter(pending_scan=False).exclude(image="").exclude(image__isnull=True).select_related(owner_field).order_by("pk")
            for comment in comments.iterator():
                name = comment.image.name
                if not name:
                    continue
                owner = getattr(comment, owner_field)
                convert_webp = get_downscale_policy(owner)[1] if owner is not None else True

                def rewrite_comment(model=model, comment=comment, name=name, convert_webp=convert_webp) -> bool:
                    rows = model.objects.all()
                    outcome = reencode_stored_field(rows, comment.pk, "image", name, max_dimension=None, convert_webp=convert_webp, only_if={"pending_scan": False})
                    if outcome is Reencoded.UNDECODABLE:
                        # Already seen by others, so the comment keeps its text and loses only the image.
                        return clear_stored_field(rows, comment.pk, "image", name)
                    return outcome is Reencoded.REPLACED

                attempt(model.__name__, comment.pk, rewrite_comment)

        for label in Label.objects.exclude(custom_icon="").exclude(custom_icon__isnull=True).order_by("pk").iterator():
            if icon_name := label.custom_icon.name:
                attempt("Label", label.pk, lambda label_id=label.pk, icon_name=icon_name: resize_stored_icon(label_id, icon_name))

        # A generated emoji SVG is the site's own template, not uploaded bytes.
        for profile in Profile.objects.exclude(avatar="").exclude(avatar__isnull=True).exclude(avatar__iendswith=".svg").order_by("pk").iterator():
            if avatar_name := profile.avatar.name:
                attempt("Profile", profile.pk, lambda profile_id=profile.pk, avatar_name=avatar_name: reencode_stored_avatar(profile_id, avatar_name))

        return rewritten, failed

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
            if extracted and image.profile is not None and not visit_logging_allowed(image.profile):
                # The upload task drops it for the same opt-out; see _process_photo_upload.
                extracted.pop("GPSInfo", None)
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
