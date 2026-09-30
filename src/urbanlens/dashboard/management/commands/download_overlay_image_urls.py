"""Download the image of every map overlay that still draws only from an external ``image_url``.

Migration 0033_v0_8_0_indexes stops while any overlay has neither a stored image nor a tile template, because it
then drops the column such an overlay draws from. This runs while it is stopped, so the model no longer declares
``image_url`` while the table still has it: the column is read and cleared with SQL.

The upload pipeline processes each linked image on its next run, as ``requeue_stalled_pending_uploads`` would. It
deletes a file it cannot open, and the overlay's image foreign key cascades, so a download whose bytes are not an
image is left unlinked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from urbanlens.dashboard.models.images.model import Image

_URL_COLUMN = "image_url"


@dataclass(frozen=True)
class _OverlayTable:
    """The overlay table's quoted identifiers, from the current model plus the column it no longer declares."""

    table: str
    pk: str
    image: str
    tile_url_template: str
    image_url: str

    @classmethod
    def quoted(cls) -> _OverlayTable:
        """Quote each identifier for this connection.

        Returns:
            The identifiers.

        Raises:
            TypeError: A field the SQL names has no column of its own.
        """
        from django.db.models import Field

        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        meta = MapImageOverlay._meta  # noqa: SLF001 - _meta is public API
        quote = connection.ops.quote_name
        columns = {}
        for name in ("id", "image", "tile_url_template"):
            field = meta.get_field(name)
            if not isinstance(field, Field) or field.column is None:
                raise TypeError(f"MapImageOverlay.{name} has no column.")
            columns[name] = quote(field.column)
        return cls(table=quote(meta.db_table), pk=columns["id"], image=columns["image"], tile_url_template=columns["tile_url_template"], image_url=quote(_URL_COLUMN))


class Command(BaseCommand):
    """Store each URL-only overlay's image locally, as a pasted URL is stored, and clear the URL."""

    help = "Download the image of each map overlay that draws only from an external URL, so migration 0033_v0_8_0_indexes can run. Deletes nothing."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--dry-run", action="store_true", help="List the overlays without downloading anything.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Download and link each overlay's image, then report what is left.

        Args:
            *args: Unused.
            **options: Parsed command options.

        Raises:
            CommandError: An overlay still draws only from an external URL when the run ends.
        """
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        with connection.cursor() as cursor:
            columns = {column.name for column in connection.introspection.get_table_description(cursor, MapImageOverlay._meta.db_table)}  # noqa: SLF001 - _meta is public API
        if _URL_COLUMN not in columns:
            self.stdout.write("Migration 0033_v0_8_0_indexes has already removed image_url; there is nothing to download.")
            return

        rows = self._url_only_rows()
        if not rows:
            self.stdout.write(self.style.SUCCESS("No overlay draws only from an external URL; migration 0033_v0_8_0_indexes can run."))
            return
        if options["dry_run"]:
            for overlay_id, url in rows:
                self.stdout.write(f"overlay {overlay_id}: {url or '(no URL)'}")
            self.stdout.write(self.style.WARNING(f"Would download {len(rows)} overlay image(s). Re-run without --dry-run to download them."))
            return

        left: dict[int, str] = {}
        for overlay_id, url in rows:
            reason = self._store(overlay_id, url)
            if reason:
                left[overlay_id] = reason
        if linked := len(rows) - len(left):
            self.stdout.write(self.style.SUCCESS(f"Stored {linked} overlay image(s) locally; the upload pipeline processes them once the migrations have run."))
        if left:
            for overlay_id, reason in sorted(left.items()):
                self.stderr.write(f"overlay {overlay_id}: {reason}")
            raise CommandError(f"{len(left)} overlay(s) still draw only from an external URL, so migration 0033_v0_8_0_indexes will still stop: {sorted(left)}. Nothing was deleted.")
        self.stdout.write(self.style.SUCCESS("No overlay draws only from an external URL; migration 0033_v0_8_0_indexes can run."))

    def _url_only_rows(self) -> list[tuple[int, str]]:
        """Every overlay with neither a stored image nor a tile template, as migration 0033_v0_8_0_indexes counts them.

        Returns:
            ``(overlay id, image_url)`` pairs, oldest first.
        """
        sql = _OverlayTable.quoted()
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {sql.pk}, {sql.image_url} FROM {sql.table} WHERE {sql.image} IS NULL AND {sql.tile_url_template} = '' ORDER BY {sql.pk}"  # noqa: S608 - identifiers from _meta
            )
            return [(int(overlay_id), str(url)) for overlay_id, url in cursor.fetchall()]

    def _store(self, overlay_id: int, url: str) -> str | None:
        """Download one overlay's image the way the overlay form stores a pasted URL, and link it.

        Args:
            overlay_id: The overlay.
            url: Its ``image_url``, as stored.

        Returns:
            None once the overlay draws from the stored image, else why it is left for a human.
        """
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
        from urbanlens.dashboard.services.map.image_overlays import OverlayImageError, image_from_external_url

        if not url.strip():
            return "it has no image, tile template or URL, so there is nothing to download"
        overlay = MapImageOverlay.objects.select_related("parent_pin__location", "parent_wiki__location", "profile").get(pk=overlay_id)
        owner = overlay.parent_pin or overlay.parent_wiki
        if owner is None:
            return f"it belongs to no pin or wiki, so {url} has nowhere to be stored"
        try:
            image = image_from_external_url(owner, overlay.profile, url.strip(), caption=overlay.name)
        except OverlayImageError as exc:
            cause = f" ({exc.__cause__})" if exc.__cause__ else ""
            return f"{url}: {exc.message}{cause}"
        if not self._is_an_image(image):
            return f"what {url} returned is not an image"
        if not self._link(overlay_id, url, image.pk):
            return "the overlay changed while this ran; run this again"
        return None

    @staticmethod
    def _is_an_image(image: Image) -> bool:
        """Whether the stored file's magic bytes are an image's, read without decoding it.

        Args:
            image: The downloaded image's row.

        Returns:
            False for bytes the upload pipeline would reject as not an image, or a file that can't be read.
        """
        from urbanlens.dashboard.services.security.content_sniffing import photo_is_not_an_image_error

        try:
            with image.image.open("rb") as stored:
                return photo_is_not_an_image_error(stored) is None
        except OSError:
            return False

    @staticmethod
    def _link(overlay_id: int, url: str, image_id: int) -> bool:
        """Point the overlay at its stored image and clear the URL, unless the row changed meanwhile.

        Args:
            overlay_id: The overlay.
            url: The ``image_url`` it was read with.
            image_id: The stored image.

        Returns:
            Whether the overlay was updated.
        """
        sql = _OverlayTable.quoted()
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {sql.table} SET {sql.image} = %s, {sql.image_url} = '' WHERE {sql.pk} = %s AND {sql.image} IS NULL AND {sql.tile_url_template} = '' AND {sql.image_url} = %s",  # noqa: S608 - identifiers from _meta
                [image_id, overlay_id, url],
            )
            return cursor.rowcount == 1
