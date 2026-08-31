"""One-off backfill: rename already-stored Image files to opaque names.

TEMPORARY - delete this command once it has been run against production.

Uploads stop leaking their filename into the served URL from the commit that
added ``Image.original_filename``/``anonymized_media_stem`` (new uploads are
named ``<year>-<random token>.<ext>``, never the uploader's own filename), but
every file already in storage still has its original name as the last segment
of an otherwise-unguessable path - and that segment is exactly what shows in
the URL of a photo shared to a wiki or DM, and what a browser's "save image
as" suggests. This walks every distinct stored name once, renames it in place
(same random directory, new opaque leaf name - the directory was already
unguessable), and backfills ``original_filename``/``filename_taken_at`` from
the name being replaced, since it is the only surviving record of it once this
runs.

Grouped by distinct stored name, not by row: sharing a pin's photos
(``services.sharing.pin_sharing``) and deduplicated re-uploads
(``services.photos.uploads``) both point several ``Image`` rows at one stored
file, and renaming it once per row would find the source already moved on the
second attempt.
"""

from __future__ import annotations

import os
import posixpath
import shutil
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import DatabaseError

from urbanlens.dashboard.models.images.model import Image, anonymized_media_stem
from urbanlens.dashboard.services.media.images import extract_filename_taken_at

if TYPE_CHECKING:
    from django.core.files.storage import Storage


def _move_stored_file(storage: Storage, old_name: str, new_name: str) -> None:
    """Move one stored file to *new_name* within the same storage backend.

    Uses a real filesystem move when the backend exposes local paths (true for
    this project's ``FileSystemStorage``), falling back to a streamed
    copy-then-delete for any backend that doesn't - a generic ``Storage`` has
    no rename primitive of its own.

    Args:
        storage: The field's storage backend.
        old_name: The file's current stored name.
        new_name: The name to move it to.

    Raises:
        OSError: The file cannot be read from or written to storage.
    """
    try:
        old_path = storage.path(old_name)
        new_path = storage.path(new_name)
    except NotImplementedError:
        with storage.open(old_name, "rb") as handle:
            storage.save(new_name, handle)
        storage.delete(old_name)
        return
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    shutil.move(old_path, new_path)


class Command(BaseCommand):
    """Rename already-stored photo/video/document files to opaque names."""

    help = "Rename already-stored Image files to opaque names, preserving the original filename on the row."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
        parser.add_argument("--limit", type=int, default=None, help="Stop after this many distinct files per pass (for a first cautious run).")

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        limit = options["limit"]

        renamed_originals = self._rename_originals(dry_run=dry_run, limit=limit)
        renamed_thumbs = self._rename_derived("thumbnail", "-thumb", dry_run=dry_run, limit=limit)

        verb = "Would rename" if dry_run else "Renamed"
        self.stdout.write(f"Done. {verb} {renamed_originals} original file(s), {renamed_thumbs} thumbnail(s).")

    def _rename_originals(self, *, dry_run: bool, limit: int | None) -> int:
        """Anonymize every distinct, not-yet-processed ``Image.image`` name.

        ``original_filename=""`` is the idempotency gate: every row starts
        there (the field is new), and this is the only writer of it, so a
        second run of this command only touches whatever a first run missed
        or failed on.
        """
        names = list(Image.objects.exclude(image="").exclude(image__isnull=True).filter(original_filename="").order_by("pk").values_list("image", flat=True).distinct())
        if limit is not None:
            names = names[:limit]
        self.stdout.write(f"Found {len(names)} distinct original file(s) to anonymize.")

        count = 0
        for old_name in names:
            representative = Image.objects.filter(image=old_name).order_by("pk").first()
            if representative is None:
                continue
            basename = posixpath.basename(old_name)
            ext = posixpath.splitext(basename)[1]
            filename_taken_at = extract_filename_taken_at(basename)
            # In-memory only, so anonymized_media_stem sees it below without a
            # premature write - the real save happens in the bulk .update().
            representative.filename_taken_at = filename_taken_at
            new_name = f"{posixpath.dirname(old_name)}/{anonymized_media_stem(representative)}{ext}"

            if dry_run:
                self.stdout.write(f"  [image] {old_name} -> {new_name}")
                count += 1
                continue

            try:
                _move_stored_file(representative.image.storage, old_name, new_name)
            except OSError as exc:
                self.stderr.write(f"  [image] FAILED to move {old_name}: {type(exc).__name__}: {exc}")
                continue
            try:
                Image.objects.filter(image=old_name).update(image=new_name, original_filename=basename, filename_taken_at=filename_taken_at)
            except DatabaseError as exc:
                self.stderr.write(f"  [image] moved but the DB update failed for {old_name} -> {new_name}: {exc}")
                continue
            count += 1
        return count

    def _rename_derived(self, field_name: str, suffix: str, *, dry_run: bool, limit: int | None) -> int:
        """Anonymize every distinct derived-file name (currently just ``thumbnail``).

        Paired to its owning row's own (already-anonymized) stem plus
        *suffix* - the same pairing :func:`write_image_thumbnail` gives a new
        upload's thumbnail. Requires :meth:`_rename_originals` to have run
        first: a row whose ``image`` is still unanonymized is skipped rather
        than paired to a name this command is about to replace anyway.

        Args:
            field_name: The derived ``ImageField`` to anonymize (e.g. ``"thumbnail"``).
            suffix: Marker appended to the paired stem (e.g. ``"-thumb"``).
            dry_run: Report without writing.
            limit: Stop after this many distinct names.

        Returns:
            How many distinct files were (or would be) renamed.
        """
        names = list(Image.objects.exclude(**{field_name: ""}).exclude(**{f"{field_name}__isnull": True}).order_by("pk").values_list(field_name, flat=True).distinct())
        if limit is not None:
            names = names[:limit]
        self.stdout.write(f"Found {len(names)} distinct {field_name} file(s) to anonymize.")

        count = 0
        for old_name in names:
            representative = Image.objects.filter(**{field_name: old_name}).order_by("pk").first()
            if representative is None or not representative.image:
                continue
            image_stem = posixpath.splitext(posixpath.basename(representative.image.name))[0]
            ext = posixpath.splitext(old_name)[1]
            new_name = f"{posixpath.dirname(old_name)}/{image_stem}{suffix}{ext}"
            if new_name == old_name:
                continue

            if dry_run:
                self.stdout.write(f"  [{field_name}] {old_name} -> {new_name}")
                count += 1
                continue

            derived_file = getattr(representative, field_name)
            try:
                _move_stored_file(derived_file.storage, old_name, new_name)
            except OSError as exc:
                self.stderr.write(f"  [{field_name}] FAILED to move {old_name}: {type(exc).__name__}: {exc}")
                continue
            try:
                Image.objects.filter(**{field_name: old_name}).update(**{field_name: new_name})
            except DatabaseError as exc:
                self.stderr.write(f"  [{field_name}] moved but the DB update failed for {old_name} -> {new_name}: {exc}")
                continue
            count += 1
        return count
