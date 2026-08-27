"""Data import service - parse and apply a UrbanLens export archive."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import os
import shutil
from typing import Any, ClassVar
import zipfile

from django.core.cache import cache

logger = logging.getLogger(__name__)

ProgressReporter = Callable[[int, int], None]

IMPORT_TTL_SECONDS = 3600
SUPPORTED_FORMATS = {"urbanlens_v1"}


def import_dir(job_id: str) -> str:
    """Return the filesystem path for a given import job."""
    from django.conf import settings as django_settings

    return os.path.join(django_settings.MEDIA_ROOT, "imports", job_id)


@dataclass
class ImportResult:
    """Summary of what the import created, skipped, and errored on."""

    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def inc_created(self, key: str, n: int = 1) -> None:
        self.created[key] = self.created.get(key, 0) + n

    def inc_skipped(self, key: str, n: int = 1) -> None:
        self.skipped[key] = self.skipped.get(key, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


class ImportJobStatus:
    """Cache-backed progress state for a user import job."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.cache_key = f"dashboard:import:{job_id}:status"

    def write(
        self,
        status: str,
        progress: int,
        message: str,
        user_id: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Write (or update) the job status in cache."""
        existing = self.read()
        data: dict[str, Any] = {"status": status, "progress": progress, "message": message}
        if user_id is not None:
            data["user_id"] = user_id
        elif "user_id" in existing:
            data["user_id"] = existing["user_id"]
        if result is not None:
            data["result"] = result
        elif "result" in existing:
            data["result"] = existing["result"]
        cache.set(self.cache_key, data, timeout=IMPORT_TTL_SECONDS)

    def read(self) -> dict[str, Any]:
        """Return the current job status dict, or an empty dict when not found."""
        return cache.get(self.cache_key) or {}

    def delete(self) -> None:
        """Remove the job status from cache."""
        cache.delete(self.cache_key)


def cleanup_import_artifacts(import_dir_path: str, job_status: ImportJobStatus | None = None) -> None:
    """Remove an import directory and optional cache-backed job status."""
    shutil.rmtree(import_dir_path, ignore_errors=True)
    if job_status is not None:
        job_status.delete()


def schedule_import_cleanup(import_dir_path: str, job_status: ImportJobStatus | None = None) -> None:
    """Schedule import cleanup through Celery; fall back to logging on enqueue failure."""
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import cleanup_import_artifacts_task

    result = safely_enqueue_task(
        cleanup_import_artifacts_task,
        import_dir_path,
        job_status.job_id if job_status is not None else None,
        countdown=IMPORT_TTL_SECONDS,
    )
    if result is None:
        logger.warning("Unable to schedule cleanup for import directory %s", import_dir_path)


def _make_step_progress_reporter(job_status: ImportJobStatus, key: str, start_pct: int, end_pct: int) -> ProgressReporter:
    """Return a throttled callback that reports (done, count) progress within [start_pct, end_pct].

    Writes to the cache-backed job status at most once per whole-percentage-point change
    (so a 4000-row step doesn't issue 4000 cache writes), but always writes on the final
    item so the step reliably lands on ``end_pct`` before the next step starts.
    """
    step_message = _STEP_MESSAGES.get(key, f"Importing {key}...")
    last_reported_pct = -1

    def report(done: int, count: int) -> None:
        nonlocal last_reported_pct
        if count <= 0:
            return
        pct = start_pct + int((done / count) * (end_pct - start_pct))
        if pct == last_reported_pct and done != count:
            return
        last_reported_pct = pct
        job_status.write("running", pct, f"{step_message} ({done}/{count})")

    return report


def run_import(user_id: int, zip_path: str, job_id: str) -> bool:
    """Parse a UrbanLens export ZIP and import data for the user.

    Idempotent: records that already exist (matched by UUID) are skipped
    rather than duplicated.

    Args:
        user_id: PK of the user to import data for.
        zip_path: Path to the uploaded export ZIP file.
        job_id: UUID string for this import job (for status tracking).

    Returns:
        True on success (even partial), False on unrecoverable error.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ObjectDoesNotExist
    from django.db import DatabaseError

    User = get_user_model()
    job_status = ImportJobStatus(job_id)

    try:
        user = User.objects.select_related("profile").get(pk=user_id)
        profile = user.profile
    except (ObjectDoesNotExist, AttributeError):
        logger.exception("Import: could not load user %s", user_id)
        job_status.write("error", 0, "Failed to load user data.")
        schedule_import_cleanup(os.path.dirname(zip_path), job_status)
        return False

    extract_dir = os.path.join(os.path.dirname(zip_path), "extracted")
    result = ImportResult()

    try:
        job_status.write("running", 5, "Validating archive...")
        data_dir = _extract_and_validate(zip_path, extract_dir, job_id, profile=profile)

        manifest = _read_json(data_dir, "manifest.json") or {}
        contents: list[str] = manifest.get("contents", [])

        steps = [k for k in _IMPORT_ORDER if k in contents]
        total = len(steps) + 1

        # Cache of UUID→PK mappings built as we go, needed for cross-references.
        pin_uuid_map: dict[str, int] = {}
        label_uuid_map: dict[str, int] = {}

        for i, key in enumerate(steps):
            step_start = 10 + int((i / total) * 80)
            step_end = 10 + int(((i + 1) / total) * 80)
            job_status.write("running", step_start, _STEP_MESSAGES.get(key, f"Importing {key}..."))

            importer = _IMPORTERS.get(key)
            if importer is None:
                continue
            report_progress = _make_step_progress_reporter(job_status, key, step_start, step_end)
            importer(profile, data_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map=label_uuid_map, report_progress=report_progress)

        job_status.write("done", 100, "Import complete!", result=result.to_dict())
        return True

    except _ImportValidationError as exc:
        logger.warning("Import validation failed for user %s: %s", user_id, exc)
        job_status.write("error", 0, str(exc))
        return False
    except (OSError, DatabaseError, ValueError):
        logger.exception("Import failed for user %s", user_id)
        job_status.write("error", 0, "Import failed. Please check the file and try again.")
        return False
    except Exception:
        # Catch-all so an unanticipated exception from an individual importer (e.g. a
        # malformed field in one row) can never leave the job stuck at "running" forever -
        # without this, the status cache is never written to "error" and the frontend
        # polls indefinitely with no feedback to the user.
        logger.exception("Unexpected import failure for user %s", user_id)
        job_status.write("error", 0, "Import failed unexpectedly. Please check the file and try again.")
        return False
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        schedule_import_cleanup(os.path.dirname(zip_path), job_status)


# -- Validation ----------------------------------------------------------------


class _ImportValidationError(Exception):
    pass


#: Ceilings on what an uploaded archive may declare before extraction even
#: starts, guarding against a crafted zip filling the disk (decompression
#: bomb) or exhausting inodes. The byte ceiling is dynamic (see
#: ``_extraction_size_ceiling``) because export archives bundle the user's
#: actual photo files, so a legitimate archive can approach the user's
#: storage quota - the floor below is only its minimum.
_EXTRACTED_BYTES_FLOOR = 2 * 1024**3
_MAX_ARCHIVE_MEMBERS = 50_000

#: Chunk size for the bounded per-member read loop in `_extract_zip_members_bounded`.
#: Bytes are only ever decompressed (via `zipfile.ZipExtFile.read()`) up to
#: whatever's actually written to disk before the running total is checked
#: against the ceiling again - this is what makes the ceiling a real, enforced
#: limit on decompressed output rather than a check against the (attacker-
#: controlled, forgeable) declared `file_size` header alone.
_ZIP_EXTRACT_CHUNK_BYTES = 1024 * 1024


def _extraction_size_ceiling(profile: Any | None) -> int:
    """Upper bound on an archive's declared uncompressed size, in bytes.

    Allows twice the profile's resolved storage quota (photo payload plus
    headroom for the JSON data and quota changes between export and import),
    never below the 2 GiB floor. Unlimited-quota users get a fixed generous
    ceiling rather than no ceiling at all - the guard exists to stop
    decompression bombs, not real exports.

    Args:
        profile: The importing profile, or None when unknown (floor-based
            fallback, used by direct callers in tests).

    Returns:
        The maximum declared uncompressed size to accept, in bytes.
    """
    from urbanlens.dashboard.services.media.storage import get_quota_bytes

    quota_bytes = get_quota_bytes(profile) if profile is not None else None
    if quota_bytes is None:
        return _EXTRACTED_BYTES_FLOOR * 32
    return max(_EXTRACTED_BYTES_FLOOR, quota_bytes * 2)


def _extract_and_validate(zip_path: str, extract_dir: str, job_id: str, profile: Any | None = None) -> str:
    """Extract the ZIP and return the path to the data directory inside it.

    Args:
        zip_path: Path to the uploaded archive.
        extract_dir: Directory to extract into.
        job_id: Import job UUID (for log context).
        profile: The importing profile, used to size the extraction ceiling.
    """
    if not os.path.exists(zip_path):
        raise _ImportValidationError("Uploaded file not found. Please try again.")

    if not zipfile.is_zipfile(zip_path):
        raise _ImportValidationError("The uploaded file is not a valid ZIP archive.")

    os.makedirs(extract_dir, exist_ok=True)
    extract_root = os.path.realpath(extract_dir)
    ceiling = _extraction_size_ceiling(profile)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise _ImportValidationError("Archive contains too many files.")
        # Fast pre-filter on the declared sizes: cheap, and rejects an obviously
        # oversized archive before any bytes are read. This is NOT the real
        # security guard, though - the declared `file_size` on each ZipInfo is
        # attacker-controlled and independent of the actual compressed payload
        # (zipfile only detects a declared-vs-actual mismatch via CRC32 *after*
        # a member is fully decompressed). The real ceiling is enforced live,
        # against actual decompressed bytes, inside
        # `_extract_zip_members_bounded` below.
        if sum(member.file_size for member in members) > ceiling:
            raise _ImportValidationError("Archive is too large to import.")
        # Guard against zip-slip path traversal. The separator is part of the
        # comparison on purpose: a bare prefix check would accept an entry
        # escaping into a SIBLING directory whose name merely starts with the
        # extract dir's (e.g. ".../job1" matching ".../job1evil/...").
        for member in members:
            dest = os.path.realpath(os.path.join(extract_root, member.filename))
            if dest != extract_root and not dest.startswith(extract_root + os.sep):
                raise _ImportValidationError("Archive contains invalid file paths.")

        _extract_zip_members_bounded(zf, members, extract_root, ceiling)

    _scan_extracted_files(extract_root)

    # The archive wraps everything in a top-level folder (urbanlens_export_YYYY-MM-DD/).
    # Find the data directory (the one containing manifest.json).
    data_dir = _find_data_dir(extract_dir)
    if data_dir is None:
        raise _ImportValidationError("Could not find manifest.json in the archive. Is this a UrbanLens export?")

    manifest = _read_json(data_dir, "manifest.json") or {}
    fmt = manifest.get("format", "")
    if fmt not in SUPPORTED_FORMATS:
        raise _ImportValidationError(
            f"Unsupported export format '{fmt}'. This file may be from an incompatible version of UrbanLens.",
        )

    return data_dir


def _extract_zip_members_bounded(
    zf: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    extract_root: str,
    ceiling: int,
) -> None:
    """Extract *members* from *zf* into *extract_root* under a hard, live-enforced byte ceiling.

    Ports the bounded-read pattern already used by
    ``archive_extractor._extract_zip`` in place of a bare ``ZipFile.extractall()``
    call: rather than trusting each member's declared (attacker-forgeable)
    ``file_size`` header, every member is decompressed and written in capped
    chunks, and the *actual* decompressed byte count accumulated so far is what
    gets checked against ``ceiling`` after every chunk - extraction aborts
    mid-stream the instant the real total is exceeded. This is what actually
    stops a small, highly-compressible ZIP (forged small ``file_size`` fields,
    huge real payload) from inflating to hundreds of GB on disk before anything
    catches it; a declared-size check alone (as previously used here) cannot,
    since zipfile only notices a declared-vs-actual mismatch via CRC32 *after*
    a member has already been fully decompressed and written.

    Symlink entries are skipped via the same Unix-mode-bit check
    ``archive_extractor._extract_zip`` uses, so a crafted archive can't plant a
    symlink for a later step to unknowingly follow.

    Args:
        zf: The already-open ZipFile.
        members: ``infolist()`` entries to extract. Directory entries are
            skipped here. Path-traversal safety of ``member.filename`` must
            already have been validated by the caller before this is invoked -
            this function trusts ``extract_root``-relative paths are safe.
        extract_root: Destination directory, already ``os.path.realpath``'d.
        ceiling: Maximum total bytes that may be written across all members.

    Raises:
        _ImportValidationError: If the actual decompressed size of the archive,
            summed across members as extraction proceeds, exceeds ``ceiling``.
    """
    total_written = 0

    for member in members:
        if member.is_dir():
            continue

        # Skip symlinks: check Unix mode bits stored in external_attr (same
        # check as archive_extractor._extract_zip). A symlink entry could
        # otherwise point extraction output at an arbitrary target path, or
        # let a later step unknowingly follow it off the extracted tree.
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            logger.warning("Skipping symlink in import archive: %s", member.filename)
            continue

        dest_path = os.path.join(extract_root, member.filename)
        parent_dir = os.path.dirname(dest_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with zf.open(member) as src, open(dest_path, "wb") as dst:
            while True:
                remaining_budget = ceiling - total_written
                if remaining_budget <= 0:
                    raise _ImportValidationError("Archive is too large to import.")
                # Never ask for more than the remaining budget: this bounds
                # the actual number of bytes zipfile decompresses in this call
                # to what's still allowed, rather than decompressing an
                # arbitrarily large chunk and discovering only afterwards that
                # it blew the ceiling.
                chunk = src.read(min(_ZIP_EXTRACT_CHUNK_BYTES, remaining_budget))
                if not chunk:
                    break
                total_written += len(chunk)
                dst.write(chunk)


def _scan_extracted_files(extract_root: str) -> None:
    """Malware-scan and content-sniff every non-JSON file extracted from the archive.

    Every extracted file is written to local disk (even if only temporarily -
    ``run_import``'s ``finally`` block removes ``extract_dir`` once the job
    ends) and the "photos/" export folder specifically will be turned into
    permanent ``Image`` rows once a photos importer exists - so scanning has
    to happen here, right after extraction and before any importer (present
    or future) ever opens these files, not deferred to whichever importer
    eventually persists them. Reuses the exact same two checks every direct
    upload endpoint already goes through (see ``images.image_upload_error``)
    rather than a bespoke check: magic-byte content-type sniffing (catching a
    file whose bytes don't match what its own extension claims) and antivirus
    scanning. JSON files are the export's own structured data (manifest,
    labels, pins, ...), not a user media upload, so they're skipped entirely.

    Args:
        extract_root: Root directory the archive was extracted into.

    Raises:
        _ImportValidationError: On the first infected file, a content/
            extension mismatch, or the antivirus scanner being unavailable.
    """
    from urbanlens.dashboard.services.security.content_sniffing import content_type_mismatch_error, guess_media_kind_from_extension
    from urbanlens.dashboard.services.security.malware_scan import MalwareScanUnavailableError, malware_error_for_upload

    for dirpath, _dirnames, filenames in os.walk(extract_root):
        for filename in filenames:
            if filename.lower().endswith(".json"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, "rb") as file_obj:
                declared_kind = guess_media_kind_from_extension(filename)
                if declared_kind is not None:
                    mismatch_error = content_type_mismatch_error(file_obj, declared_kind)
                    if mismatch_error:
                        raise _ImportValidationError(f"'{filename}' in the import archive doesn't match its file type and the import was rejected.")

                try:
                    malware_error = malware_error_for_upload(file_obj)
                except MalwareScanUnavailableError as exc:
                    raise _ImportValidationError("Our antivirus scanner is temporarily unavailable. Please try again shortly.") from exc
                if malware_error:
                    raise _ImportValidationError(f"'{filename}' in the import archive was flagged as malicious and the import was rejected.")


def _find_data_dir(root: str) -> str | None:
    """Walk the extracted directory tree to locate manifest.json."""
    for dirpath, _dirnames, filenames in os.walk(root):
        if "manifest.json" in filenames:
            return dirpath
    return None


def _read_json(data_dir: str, filename: str) -> Any:
    """Read and parse a JSON file from the data directory; return None if missing."""
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# -- Individual importers -------------------------------------------------------


def _import_labels(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import user-owned label definitions. Global labels are matched by name."""
    from uuid import UUID, uuid4

    from urbanlens.dashboard.models.labels.model import Label

    # Fall back to the pre-rename filename so backup archives exported before the
    # Badge -> Label rename still import cleanly.
    rows = _read_json(data_dir, "labels.json") or _read_json(data_dir, "badges.json")
    if not rows:
        return
    total_rows = len(rows)

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        uuid_str = row.get("uuid", "")
        name = row.get("name", "").strip()
        if not name:
            result.inc_skipped("labels")
            continue

        kind = row.get("kind", "tag")
        is_user_label = row.get("is_user_label", True)

        try:
            label_uuid = UUID(uuid_str)
        except (ValueError, AttributeError, TypeError):
            label_uuid = uuid4()

        if is_user_label:
            # Match by UUID first (re-importing the same export), then by name+kind
            # (the import may be re-run against data that was already imported, or
            # the export UUID may not round-trip - either way, don't duplicate).
            # name__iexact, not name: labels are unique on (lower(name), profile,
            # kind) since migration 0043, so an exact-match lookup would miss
            # "Abandoned" while importing "abandoned" and then fail the whole
            # import on the constraint instead of skipping the duplicate.
            existing = Label.objects.filter(uuid=label_uuid, profile=profile).first() or Label.objects.filter(profile=profile, name__iexact=name, kind=kind).first()
            if existing:
                label_uuid_map[uuid_str] = existing.pk
                result.inc_skipped("labels")
                continue

            label = Label.objects.create(
                uuid=label_uuid,
                profile=profile,
                name=name,
                description=row.get("description") or "",
                color=row.get("color") or None,
                icon=row.get("icon") or None,
                kind=kind,
                order=row.get("order", 0),
            )
            label_uuid_map[uuid_str] = label.pk
            result.inc_created("labels")
        else:
            # Global label: match by name+kind first, then fall back to a user-owned
            # label with the same name+kind, then create as user-owned if neither exists.
            existing = Label.objects.filter(profile__isnull=True, name__iexact=name, kind=kind).first()
            if existing:
                label_uuid_map[uuid_str] = existing.pk
                result.inc_skipped("labels")
            else:
                # Re-create as a user-owned label (global doesn't exist on this instance).
                user_existing = Label.objects.filter(profile=profile, name__iexact=name, kind=kind).first()
                if user_existing:
                    label_uuid_map[uuid_str] = user_existing.pk
                    result.inc_skipped("labels")
                else:
                    label = Label.objects.create(
                        uuid=label_uuid,
                        profile=profile,
                        name=name,
                        description=row.get("description") or "",
                        color=row.get("color") or None,
                        icon=row.get("icon") or None,
                        kind=kind,
                        order=row.get("order", 0),
                    )
                    label_uuid_map[uuid_str] = label.pk
                    result.inc_created("labels")

    # Second pass: wire up parent relationships now that all labels exist.
    for row in rows:
        uuid_str = row.get("uuid", "")
        if uuid_str not in label_uuid_map:
            continue
        parent_uuids = row.get("parent_uuids", [])
        if not parent_uuids:
            continue
        try:
            label = Label.objects.get(pk=label_uuid_map[uuid_str])
        except Label.DoesNotExist:
            continue
        parent_pks = [label_uuid_map[u] for u in parent_uuids if u in label_uuid_map]
        if parent_pks:
            label.parents.add(*parent_pks)


def _import_pins(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import user pins.

    Pins are imported as bare coordinates, exactly as if the user had dropped
    a new pin manually or imported a Google Takeout file. Location resolution
    (matching an existing shared Location nearby, or creating a new one)
    happens inside ``Pin.objects.get_nearby_or_create``. No community wiki,
    boundary, or external-API work happens at import time: wikis are created
    explicitly by the user from the pin detail page, and default boundaries
    are generated lazily when a pin detail page is first viewed.

    Pins are deduped per-profile by proximity via ``Pin.objects.get_nearby_or_create``
    (the same helper the Google Takeout importer uses) rather than inserted
    directly. Multiple exported pins commonly resolve to the same effective
    coordinate (e.g. several pins that all rely on one shared Location for
    placement), which would otherwise collide with the one-root-pin-per-point
    per-profile database constraint.

    A pin's review rating and private article are only ever created here (never
    on a re-import that skips an already-existing pin), matching the same
    "create-time only" treatment as labels below.
    """
    from django.db import IntegrityError

    from urbanlens.dashboard.models.abstract.choices import SecurityLevel
    from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
    from urbanlens.dashboard.models.pin.model import Pin

    rows = _read_json(data_dir, "pins.json")
    if not rows:
        return
    total_rows = len(rows)
    security_level_values = set(SecurityLevel.values)

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        uuid_str = row.get("uuid", "")

        # Idempotency: skip pins that already exist FOR THIS USER. The
        # profile scope is load-bearing: the archive is user-supplied, so a
        # uuid belonging to another user's pin must not enter pin_uuid_map -
        # later steps (visit history) create rows against the mapped pks.
        existing = Pin.objects.filter(uuid=uuid_str, profile=profile).first() if uuid_str else None
        if existing:
            pin_uuid_map[uuid_str] = existing.pk
            result.inc_skipped("pins")
            continue

        lat = row.get("latitude")
        lng = row.get("longitude")
        if lat is None or lng is None:
            result.warnings.append(f"Could not import pin '{row.get('name', uuid_str)}': missing coordinates.")
            result.inc_skipped("pins")
            continue

        defaults: dict[str, Any] = {
            "name": row.get("name") or None,
            "description": row.get("description") or "",
            "icon": row.get("icon") or None,
            "color": row.get("color") or None,
            "priority": int(row.get("priority", 0)),
            "vulnerability": int(row.get("vulnerability", 0)),
            "danger": int(row.get("danger", 0)),
            "pin_type": row.get("pin_type", "location"),
            "detail_bg_color": row.get("detail_bg_color") or None,
            "detail_bg_opacity": int(row.get("detail_bg_opacity", 80)),
            "detail_border_color": row.get("detail_border_color") or None,
            "detail_border_opacity": int(row.get("detail_border_opacity", 100)),
        }
        security = row.get("security") or {}
        for field_name, _label in SECURITY_FIELDS:
            value = security.get(field_name)
            if value in security_level_values:
                defaults[field_name] = value
        # Only carry the archive's uuid onto the new pin when it isn't
        # already taken by another user's pin (uuid is globally unique);
        # otherwise import as a fresh pin. pin_uuid_map still keys on the
        # archive's uuid either way - it exists to resolve the archive's own
        # internal cross-references.
        if uuid_str and not Pin.objects.filter(uuid=uuid_str).exists():
            defaults["uuid"] = uuid_str

        try:
            pin, created = Pin.objects.get_nearby_or_create(lat, lng, profile, defaults=defaults)
        except (IntegrityError, ValueError, TypeError):
            logger.warning("Failed to import pin %s", uuid_str, exc_info=True)
            result.warnings.append(f"Could not import pin '{row.get('name', uuid_str)}'.")
            continue

        if pin is None:
            result.inc_skipped("pins")
            continue

        if uuid_str:
            pin_uuid_map[uuid_str] = pin.pk

        if not created:
            result.inc_skipped("pins")
            continue

        result.inc_created("pins")

        # Assign labels. "badge_uuids" is the pre-rename key, kept for old backup archives.
        for label_uuid in row.get("label_uuids") or row.get("badge_uuids", []):
            if label_uuid in label_uuid_map:
                pin.labels.add(label_uuid_map[label_uuid])

        for alias_row in row.get("aliases") or []:
            _restore_pin_alias(pin, alias_row)

        rating = row.get("rating")
        if isinstance(rating, int) and 0 <= rating <= 5:
            from urbanlens.dashboard.models.reviews.model import Review

            Review.objects.create(profile=profile, pin=pin, rating=rating)

        article_data = row.get("article") or {}
        content = article_data.get("content")
        if content:
            from urbanlens.dashboard.services.wiki.articles import save_article

            save_article(editor=profile, content=content, edit_summary="Imported", pin=pin)


def _restore_pin_alias(pin: Any, alias_row: Any) -> None:
    """Re-create one user-authored alias on a freshly imported pin.

    Only ``source == "user"`` aliases are restored. An ``official`` alias is
    written by an external name provider and re-synced by that provider against
    whatever *this* instance resolves the pin to, so importing one would freeze
    a stale third-party name in place. Matching is case-insensitive because the
    ``(lower(name), pin)`` uniqueness constraint is - an exact-match check would
    miss a case variant and turn a duplicate into a constraint error.

    Args:
        pin: The newly created Pin the alias belongs to.
        alias_row: One entry from the exported pin's ``aliases`` list.
    """
    from django.db import IntegrityError

    from urbanlens.dashboard.models.aliases.model import AliasSource, AliasType, PinAlias

    if not isinstance(alias_row, dict):
        return
    if (alias_row.get("source") or AliasSource.USER) != AliasSource.USER:
        return
    name = str(alias_row.get("name") or "").strip()[:255]
    if not name:
        return
    kind = alias_row.get("kind")
    if kind not in AliasType.values:
        kind = AliasType.ALTERNATE
    if PinAlias.objects.filter(pin=pin, name__iexact=name).exists():
        return
    try:
        PinAlias.objects.create(pin=pin, name=name, kind=kind, source=AliasSource.USER)
    except IntegrityError:
        # PinAlias.save() sanitizes the name, so a name that looked distinct in
        # the archive can collide with an existing alias once normalized.
        logger.debug("Skipped duplicate pin alias %r on pin %s", name, pin.pk)


def _import_visit_history(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import visit history records, skipping duplicates by (pin, visited_at)."""
    from django.utils.dateparse import parse_datetime

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit
    from urbanlens.dashboard.services.visits.visits import visit_logging_allowed

    rows = _read_json(data_dir, "visit_history.json")
    if not rows:
        return
    total_rows = len(rows)

    if not visit_logging_allowed(profile):
        result.inc_skipped("visit_history", total_rows)
        return

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        pin_uuid = row.get("pin_uuid", "")
        visited_at_str = row.get("visited_at", "")

        pin_pk = pin_uuid_map.get(pin_uuid)
        if pin_pk is None:
            # Try to find the pin directly (e.g. restoring to same user).
            pin = Pin.objects.filter(uuid=pin_uuid, profile=profile).first()
            if pin is None:
                result.inc_skipped("visit_history")
                continue
            pin_pk = pin.pk
            pin_uuid_map[pin_uuid] = pin_pk

        visited_at = parse_datetime(visited_at_str)
        if visited_at is None:
            result.warnings.append(f"Skipped visit with invalid date '{visited_at_str}'.")
            result.inc_skipped("visit_history")
            continue

        if PinVisit.objects.filter(pin_id=pin_pk, visited_at=visited_at).exists():
            result.inc_skipped("visit_history")
            continue

        PinVisit.objects.create(
            pin_id=pin_pk,
            visited_at=visited_at,
            notes=row.get("notes") or None,
            source=row.get("source", "manual"),
        )
        result.inc_created("visit_history")


def _import_connections(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import friendship connections as fresh friend requests.

    The archive is user-supplied input, so its rows are treated as requests
    rather than facts: an import may only re-create actions the importing
    user could take themselves through the UI. Each outgoing row becomes a
    new friend REQUEST via ``Friendship.request`` - the chokepoint that
    enforces the community-enabled and existing/blocked-row guards - except
    an outgoing BLOCK, which is restored directly since blocking is a
    unilateral action the importer owns. The exported status, permissions,
    and incoming rows are otherwise ignored: honoring them would let a
    crafted archive forge an ACCEPTED friendship (or a row "from" another
    user) and grant itself friend-level access to that user's data.
    """
    from django.db.models import Q

    from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
    from urbanlens.dashboard.models.friendship.model import Friendship
    from urbanlens.dashboard.models.profile.model import Profile

    rows = _read_json(data_dir, "connections.json")
    if not rows:
        return
    total_rows = len(rows)

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        other_uuid = row.get("other_user_uuid") or ""
        direction = row.get("direction", "outgoing")

        if not other_uuid or direction != "outgoing":
            # Outgoing not-yet-accepted rows export with identity withheld
            # (nothing to act on), and an incoming row records the OTHER
            # user's action - it cannot be re-created on their behalf; they
            # must send the request themselves.
            result.inc_skipped("connections")
            continue

        other_profile = Profile.objects.filter(uuid=other_uuid).first()
        if other_profile is None or other_profile.pk == profile.pk:
            result.warnings.append(
                f"Skipped connection with '{row.get('other_username', other_uuid)}': user not found on this instance.",
            )
            result.inc_skipped("connections")
            continue

        already_connected = Friendship.objects.filter(
            Q(from_profile=profile, to_profile=other_profile) | Q(from_profile=other_profile, to_profile=profile),
        ).exists()
        if already_connected:
            result.inc_skipped("connections")
            continue

        relationship_type = row.get("relationship_type", "")
        if relationship_type not in FriendshipType.values:
            relationship_type = FriendshipType.FRIEND

        try:
            if row.get("status") == FriendshipStatus.BLOCKED:
                Friendship.objects.create(
                    from_profile=profile,
                    to_profile=other_profile,
                    status=FriendshipStatus.BLOCKED,
                    relationship_type=relationship_type,
                )
                result.inc_created("connections")
                continue

            friendship = Friendship.request(from_profile=profile, to_profile=other_profile, relationship_type=relationship_type)
        except Exception:
            logger.warning("Failed to import connection %s → %s", profile, other_profile, exc_info=True)
            result.warnings.append(f"Could not import connection with '{row.get('other_username', other_uuid)}'.")
            continue

        if friendship is not None:
            result.inc_created("connections")
        else:
            result.inc_skipped("connections")


#: Settings that are plain scalar Profile fields, copied through as-is when
#: present (booleans/ints round-trip through JSON natively; choice fields are
#: separately validated via ``_safe_set`` below since a foreign instance's
#: choices module could differ in a future version).
_SETTINGS_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "cluster_radius",
    "use_pin_cache",
    "map_default_zoom",
    "remembered_map_zoom",
    "markup_fill_color",
    "markup_fill_opacity",
    "markup_border_color",
    "markup_border_opacity",
    "pin_detail_map_height",
    "media_gallery_sort",
    "show_wiki_cover_photos",
    "auto_create_pin_article_from_wikipedia",
    "external_apis_enabled",
)

#: Settings stored as DecimalField on Profile - imported only when parseable.
_SETTINGS_DECIMAL_FIELDS: tuple[str, ...] = (
    "map_center_latitude",
    "map_center_longitude",
    "map_custom_latitude",
    "map_custom_longitude",
    "remembered_map_lat",
    "remembered_map_lng",
)

#: Nested boolean/int groups in settings.json - flattened onto Profile fields
#: of the same name (each group mirrors one settings-page section).
_SETTINGS_GROUPS: tuple[str, ...] = ("ai", "keyword_tagging", "photos", "places_layers", "tracking", "community")

#: Explicit allowlist of field names per group, mirroring exactly what
#: ``_export_settings`` writes into each group. The archive is user-supplied
#: input, so a plain ``hasattr(Profile, key)`` check is not safe here: it
#: would also be true for fields never meant to be settable this way -
#: including ``user`` (the OneToOneField to auth.User) and internal
#: bookkeeping fields such as ``deletion_requested_at``/``tos_accepted_at``/
#: ``profile_setup_complete``/``slug``/``primary_email_normalized``. A
#: hand-crafted export file smuggling e.g. ``{"community": {"user": 1}}``
#: must not be able to repoint identity/bookkeeping columns via this path.
#: ``sync_aliases`` is deliberately omitted from ``community`` - it's a
#: choice field, validated separately via ``_safe_set`` below.
_SETTINGS_GROUP_FIELDS: dict[str, frozenset[str]] = {
    "ai": frozenset({"ai_enabled", "ai_label_tags", "ai_label_categories", "ai_label_statuses"}),
    "keyword_tagging": frozenset({"keyword_tagging_enabled", "keyword_label_tags", "keyword_label_categories", "keyword_label_statuses"}),
    "photos": frozenset({"generate_photo_keywords", "image_downscale_max_dimension", "video_downscale_max_height"}),
    "places_layers": frozenset({"places_google_enabled", "places_nps_enabled", "places_wikipedia_enabled"}),
    "tracking": frozenset({"track_pin_visits", "track_routes", "track_geolocation"}),
    "community": frozenset({"community_enabled", "sync_rating_to_wiki", "sync_vulnerability_to_wiki", "sync_priority_to_wiki", "sync_danger_to_wiki"}),
}

#: Choice fields validated against the model's own choices before being applied.
_SETTINGS_CHOICE_FIELDS: tuple[str, ...] = (
    "theme_mode",
    "guidance_level",
    "distance_units",
    "map_dark_mode",
    "default_map_view",
    "map_center_mode",
)

_PRIVACY_FIELDS: tuple[str, ...] = (
    "profile_visibility",
    "comment_visibility",
    "friend_request_visibility",
    "photo_upload_visibility",
    "viewer_photo_filter",
    "trip_pin_location_visibility",
    "contact_visibility",
    "direct_message_visibility",
    "online_status_visibility",
    "read_receipt_visibility",
    "typing_indicator_visibility",
    "common_pins_visibility",
    "direct_message_delete_after",
)


def _import_settings(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import user settings, overwriting the current profile settings."""
    from decimal import Decimal, InvalidOperation

    from urbanlens.dashboard.models.profile.model import Profile

    data = _read_json(data_dir, "settings.json")
    if not data:
        return

    privacy = data.get("privacy", {})
    update_fields: dict[str, Any] = {}

    for field_name in _SETTINGS_CHOICE_FIELDS:
        _safe_set(update_fields, field_name, data, Profile, field_name)

    for field_name in _SETTINGS_PASSTHROUGH_FIELDS:
        if field_name in data and data[field_name] is not None:
            update_fields[field_name] = data[field_name]

    for field_name in _SETTINGS_DECIMAL_FIELDS:
        raw = data.get(field_name)
        if raw is None:
            continue
        try:
            update_fields[field_name] = Decimal(str(raw))
        except InvalidOperation:
            continue

    for group_name in _SETTINGS_GROUPS:
        group = data.get(group_name) or {}
        allowed_fields = _SETTINGS_GROUP_FIELDS.get(group_name, frozenset())
        update_fields.update({field_name: value for field_name, value in group.items() if field_name in allowed_fields})

    if "sync_aliases" in (data.get("community") or {}):
        _safe_set(update_fields, "sync_aliases", data["community"], Profile, "sync_aliases")

    for field_name in _PRIVACY_FIELDS:
        if field_name in privacy:
            update_fields[field_name] = privacy[field_name]
    if "allow_friend_recommendations" in privacy:
        update_fields["allow_friend_recommendations"] = bool(privacy["allow_friend_recommendations"])

    if update_fields:
        Profile.objects.filter(pk=profile.pk).update(**update_fields)
        result.inc_created("settings")
    else:
        result.inc_skipped("settings")

    _import_notification_preferences(profile, data.get("notification_preferences") or {}, result)


def _import_notification_preferences(profile: Any, data: dict[str, Any], result: ImportResult) -> None:
    """Apply exported per-notification-type delivery preferences, if present."""
    if not data:
        return
    from urbanlens.dashboard.models.notifications.model import NotificationPreference

    fields = {f.name for f in NotificationPreference._meta.get_fields() if getattr(f, "concrete", False)}  # noqa: SLF001
    update_fields = {name: value for name, value in data.items() if name in fields and name not in {"id", "profile", "created", "updated"}}
    if not update_fields:
        return
    NotificationPreference.objects.update_or_create(profile=profile, defaults=update_fields)


def _safe_set(
    update_fields: dict[str, Any],
    key: str,
    data: dict[str, Any],
    model_class: Any,
    field_name: str,
) -> None:
    """Set field_name in update_fields when the key is present in data and is a valid choice."""
    if key not in data:
        return
    value = data[key]
    try:
        field = model_class._meta.get_field(field_name)  # noqa: SLF001
        choices = [c[0] for c in (field.choices or [])]
        if not choices or value in choices:
            update_fields[field_name] = value
    except Exception:
        logger.debug("Skipped setting %s=%r: could not validate choices", field_name, value)


def _import_pin_lists(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import pin lists (idempotent by UUID) and their pin membership rows.

    Smart-list config (``smart_filter``/``smart_boundary``) is copied as-is;
    it is not re-evaluated against the importing profile's pins here - the
    normal smart-membership signal/service picks it back up the next time a
    member pin is saved or the list is edited.
    """
    from django.db import IntegrityError

    from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem

    rows = _read_json(data_dir, "pin_lists.json")
    if not rows:
        return
    total_rows = len(rows)

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        uuid_str = row.get("uuid", "")

        existing = PinList.objects.filter(uuid=uuid_str).first() if uuid_str else None
        if existing:
            result.inc_skipped("pin_lists")
            continue

        smart_boundary = None
        if row.get("smart_boundary"):
            from urbanlens.dashboard.services.geo.geo import parse_multipolygon_geojson

            try:
                smart_boundary = parse_multipolygon_geojson(row["smart_boundary"])
            except (ValueError, TypeError):
                result.warnings.append(f"Could not import boundary for list '{row.get('name', uuid_str)}'.")

        defaults: dict[str, Any] = {
            "name": row.get("name") or "Imported list",
            "description": row.get("description") or "",
            "is_smart": bool(row.get("is_smart")),
            "smart_filter": row.get("smart_filter"),
            "smart_boundary": smart_boundary,
        }
        if uuid_str:
            defaults["uuid"] = uuid_str

        try:
            pin_list = PinList.objects.create(profile=profile, **defaults)
        except IntegrityError:
            # Name collision with an existing list - suffix rather than overwrite it.
            defaults["name"] = f"{defaults['name']} (imported)"
            pin_list = PinList.objects.create(profile=profile, **defaults)

        items = [
            PinListItem(
                pin_list=pin_list,
                pin_id=pin_uuid_map[item_row["pin_uuid"]],
                order=item_row.get("order", 0),
                added_via=item_row.get("added_via", PinListItem.ADDED_MANUAL),
            )
            for item_row in row.get("items", [])
            if item_row.get("pin_uuid") in pin_uuid_map
        ]
        if items:
            PinListItem.objects.bulk_create(items)

        result.inc_created("pin_lists")


def _import_custom_fields(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import custom field definitions, plus values for pin-targeted fields.

    Field *definitions* (name/type/config) are always imported (idempotent by
    profile+entity_type+name, matching the DB's own uniqueness constraint) -
    they're useful on their own even with no data. Values are only re-created
    for entity_type=pin, since that's the only target type this import can
    resolve a real local object for (photos/people/maps aren't imported by
    any other step); other entity types' values are skipped with a warning
    rather than silently dropped.
    """
    from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType, CustomFieldValue

    rows = _read_json(data_dir, "custom_fields.json")
    if not rows:
        return
    total_rows = len(rows)
    skipped_value_entities: set[str] = set()

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        entity_type = row.get("entity_type", "")
        name = (row.get("name") or "").strip()
        if entity_type not in CustomFieldEntity.values or not name:
            result.inc_skipped("custom_fields")
            continue

        field, created = CustomField.objects.get_or_create(
            profile=profile,
            entity_type=entity_type,
            name=name,
            defaults={
                "field_type": row.get("field_type", CustomFieldType.TEXT),
                "style": row.get("style") or "",
                "config": row.get("config") or {},
            },
        )
        if created:
            result.inc_created("custom_fields")
        else:
            result.inc_skipped("custom_fields")

        if entity_type != CustomFieldEntity.PIN:
            if row.get("values"):
                skipped_value_entities.add(entity_type)
            continue

        for value_row in row.get("values", []):
            pin_pk = pin_uuid_map.get(value_row.get("target_uuid", ""))
            if pin_pk is None:
                continue
            if CustomFieldValue.objects.filter(field=field, pin_id=pin_pk).exists():
                result.inc_skipped("custom_field_values")
                continue
            value_obj = CustomFieldValue(field=field, pin_id=pin_pk)
            if _apply_exported_custom_field_value(value_obj, field.field_type, value_row.get("value"), pin_uuid_map):
                value_obj.save()
                result.inc_created("custom_field_values")
            else:
                result.inc_skipped("custom_field_values")

    for entity_type in sorted(skipped_value_entities):
        label = dict(CustomFieldEntity.choices).get(entity_type, entity_type)
        result.warnings.append(f"Custom field values for {label} were not re-created - only pin-targeted values can be imported.")


def _apply_exported_custom_field_value(value_obj: Any, field_type: str, exported: Any, pin_uuid_map: dict[str, int]) -> bool:
    """Set the typed column on ``value_obj`` from an ``export_value()``-shaped payload.

    Args:
        value_obj: An unsaved CustomFieldValue with ``field``/target already set.
        field_type: The owning field's ``CustomFieldType``.
        exported: The value as written by ``CustomFieldValue.export_value()``.
        pin_uuid_map: Archive uuid -> local pk, for resolving pin references.

    Returns:
        True when a value was applied, False when it couldn't be (caller should skip).
    """
    from decimal import Decimal, InvalidOperation

    from django.utils.dateparse import parse_date, parse_time

    from urbanlens.dashboard.models.custom_fields.model import CustomFieldType

    if exported is None:
        return False

    if field_type == CustomFieldType.NUMBER:
        try:
            value_obj.value_number = Decimal(str(exported))
        except InvalidOperation:
            return False
    elif field_type == CustomFieldType.DATE:
        parsed_date = parse_date(str(exported))
        if parsed_date is None:
            return False
        value_obj.value_date = parsed_date
    elif field_type == CustomFieldType.TIME:
        parsed_time = parse_time(str(exported))
        if parsed_time is None:
            return False
        value_obj.value_time = parsed_time
    elif field_type == CustomFieldType.CHECKBOX:
        value_obj.value_boolean = bool(exported)
    elif field_type == CustomFieldType.REFERENCE:
        if not isinstance(exported, dict) or exported.get("kind") != "pin":
            return False
        target_pk = pin_uuid_map.get(exported.get("uuid", ""))
        if target_pk is None:
            return False
        value_obj.ref_pin_id = target_pk
    else:
        value_obj.value_text = str(exported)
    return True


def _safe_uuid(value: Any) -> str | None:
    """Return ``value`` when it parses as a UUID string, else None."""
    import uuid as uuid_module

    try:
        return str(uuid_module.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _resolve_import_target(profile: Any, row: dict[str, Any], pin_uuid_map: dict[str, int]) -> tuple[int | None, Any | None, bool]:
    """Resolve a comment/photo row's exported target to a local pin pk or Wiki.

    Matches on ``target_uuid`` only (names are neither unique nor stable). Pin
    targets must resolve to the importing user's OWN pin - the archive is
    user-supplied, so a uuid pointing at someone else's pin must never attach
    content there. Wiki targets must pass the same access check the wiki page
    itself enforces (``location_visible_to``): a crafted archive must not
    attach content to a wiki its owner couldn't even see.

    Args:
        profile: The importing profile.
        row: The exported row (reads ``target_type``/``target_uuid``).
        pin_uuid_map: Archive pin uuid -> local pk, built by the pins step.

    Returns:
        ``(pin_pk, wiki, resolved)`` - ``resolved`` False means the row named
        a target that could not (or must not) be matched here; ``(None, None,
        True)`` means the row genuinely had no target.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

    target_type = row.get("target_type") or ""
    target_uuid = _safe_uuid(row.get("target_uuid"))
    if not target_type:
        return None, None, True
    if target_uuid is None:
        return None, None, False
    if target_type == "pin":
        pin_pk = pin_uuid_map.get(target_uuid)
        if pin_pk is None:
            pin = Pin.objects.filter(uuid=target_uuid, profile=profile).first()
            if pin is None:
                return None, None, False
            pin_pk = pin.pk
            pin_uuid_map[target_uuid] = pin_pk
        return pin_pk, None, True
    if target_type == "location":
        wiki = Wiki.objects.filter(uuid=target_uuid).select_related("location").first()
        if wiki is None or not location_visible_to(wiki.location, profile):
            return None, None, False
        return None, wiki, True
    return None, None, False


def _apply_exported_created(instance: Any, created_raw: Any) -> None:
    """Backdate an imported row's ``created`` to the exported timestamp.

    ``created`` is ``auto_now_add`` so it can't be set at create time; a
    queryset ``update`` after the fact preserves the original ordering
    (comment threads, message history) without fighting the field definition.
    Unparseable timestamps are simply left at import time.
    """
    from django.utils.dateparse import parse_datetime

    created = parse_datetime(str(created_raw or ""))
    if created is not None:
        type(instance).objects.filter(pk=instance.pk).update(created=created)


def _import_comments(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import the user's own pin/wiki comments (Notes), matched by ``target_uuid``.

    A comment only means something attached to its target, so rows whose
    target can't be resolved (pin not in this import or not the user's own;
    wiki absent on this instance or not visible to the user - the same
    ``location_visible_to`` gate the wiki page enforces) are skipped with a
    warning rather than imported as orphans. Idempotent per comment uuid.
    Only archives from before the ``target_uuid`` export field can hit the
    "no target_uuid" skip - name-based matching is deliberately not attempted.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.services.core.text_limits import MAX_COMMENT_TEXT_LENGTH

    rows = _read_json(data_dir, "comments.json")
    if not rows:
        return
    total_rows = len(rows)
    unresolved = 0

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and Comment.objects.filter(uuid=uuid_str, profile=profile).exists():
            result.inc_skipped("comments")
            continue

        text = (row.get("text") or "").strip()
        if not text or len(text) > MAX_COMMENT_TEXT_LENGTH:
            result.inc_skipped("comments")
            continue

        pin_pk, wiki, resolved = _resolve_import_target(profile, row, pin_uuid_map)
        if not resolved or (pin_pk is None and wiki is None):
            unresolved += 1
            result.inc_skipped("comments")
            continue

        # Content-level idempotency backstop: when the archive's uuid is
        # already taken by ANOTHER row (same-instance import - the exporter's
        # own comment still holds it), the imported copy gets a fresh uuid, so
        # the uuid check above can never catch a re-import. An identical
        # (target, text, exported-created) row is the same comment.
        from django.utils.dateparse import parse_datetime

        exported_created = parse_datetime(str(row.get("created") or ""))
        if exported_created is not None and Comment.objects.filter(profile=profile, pin_id=pin_pk, wiki=wiki, text=text, created=exported_created).exists():
            result.inc_skipped("comments")
            continue

        comment = Comment(profile=profile, pin_id=pin_pk, wiki=wiki, text=text)
        # Never inherit a uuid already taken by someone else's row - the
        # archive is user-supplied input.
        if uuid_str and not Comment.objects.filter(uuid=uuid_str).exists():
            comment.uuid = uuid_str
        comment.save()
        _apply_exported_created(comment, row.get("created"))
        result.inc_created("comments")

    if unresolved:
        result.warnings.append(f"Skipped {unresolved} comment(s) whose pin or wiki could not be matched on this instance.")


def _import_photos(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Import the archive's ``photos/`` files back into storage.

    Every archive file was already malware-scanned at extraction
    (``_scan_extracted_files``). Each photo re-enters storage through the
    same quota and max-file-size checks a fresh upload gets; ones that don't
    fit are skipped with a warning rather than blowing the quota. A photo
    whose target can't be resolved still imports as an unattached upload
    (the data is the user's own either way - unlike a comment, a photo
    doesn't need a target to be meaningful). Idempotent per image uuid.
    """
    from decimal import Decimal, InvalidOperation

    from django.core.files import File

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.media.storage import file_size_error_for_upload, per_profile_upload_lock, quota_error_for_upload

    rows = _read_json(data_dir, os.path.join("photos", "metadata.json"))
    if not rows:
        return
    photos_dir = os.path.join(data_dir, "photos")
    total_rows = len(rows)
    missing_files = 0
    over_quota = 0

    def _decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)
        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and Image.objects.filter(uuid=uuid_str, profile=profile).exists():
            result.inc_skipped("photos")
            continue

        # basename() guards against traversal - metadata.json is part of the
        # user-supplied archive, so its filenames are untrusted input.
        filename = os.path.basename(str(row.get("filename") or ""))
        src_path = os.path.join(photos_dir, filename) if filename else ""
        if not filename or not os.path.isfile(src_path):
            missing_files += 1
            result.inc_skipped("photos")
            continue

        size = os.path.getsize(src_path)
        with per_profile_upload_lock(profile):
            if file_size_error_for_upload(size) or quota_error_for_upload(profile, size):
                over_quota += 1
                result.inc_skipped("photos")
                continue

            pin_pk, wiki, _resolved = _resolve_import_target(profile, row, pin_uuid_map)

            media_type = row.get("media_type") if row.get("media_type") in MediaKind.values else MediaKind.PHOTO
            image = Image(
                profile=profile,
                pin_id=pin_pk,
                wiki=wiki,
                caption=(row.get("caption") or "")[:500] or None,
                media_type=media_type,
                latitude=_decimal(row.get("latitude")),
                longitude=_decimal(row.get("longitude")),
                file_size=size,
            )
            if uuid_str and not Image.objects.filter(uuid=uuid_str).exists():
                image.uuid = uuid_str
            with open(src_path, "rb") as fh:
                image.image.save(filename, File(fh), save=True)

        label_pks = [label_uuid_map[label_uuid] for label_uuid in (row.get("label_uuids") or []) if label_uuid in label_uuid_map]
        if label_pks:
            image.labels.add(*label_pks)
        _apply_exported_created(image, row.get("created"))
        result.inc_created("photos")

    if missing_files:
        result.warnings.append(f"Skipped {missing_files} photo(s) whose file was missing from the archive.")
    if over_quota:
        result.warnings.append(f"Skipped {over_quota} photo(s) that would exceed your storage quota or the maximum upload size.")


def _import_trips(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Re-create the trips the user owned; members become fresh invitations.

    Requests-not-facts, exactly like ``_import_connections``: only trips the
    user *created* are rebuilt (a membership in someone else's trip records
    THEIR trip - it cannot be reconstructed on their behalf), and exported
    members are re-invited through the same guards the trip UI applies
    (connections only, ``max_trip_members`` cap, ``STATUS_INVITED`` so each
    person still accepts for themselves). The upcoming-trips cap is honored
    for trips with a future start date. Idempotent per trip uuid.
    """
    from django.utils.dateparse import parse_date

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.trips.model import Trip, TripMembership
    from urbanlens.dashboard.services.social.connections import get_connections

    rows = _read_json(data_dir, "trips.json")
    if not rows:
        return
    total_rows = len(rows)

    site_settings = SiteSettings.get_current()
    max_upcoming = site_settings.max_upcoming_trips_per_user
    max_members = site_settings.max_trip_members
    upcoming_count = Trip.objects.upcoming(profile).count()
    connection_uuids = {str(connection.uuid) for connection in get_connections(profile)}
    not_owned = 0

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)

        if not row.get("is_creator", False):
            not_owned += 1
            result.inc_skipped("trips")
            continue

        name = (row.get("name") or "").strip()[:255]
        if not name:
            result.inc_skipped("trips")
            continue

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str:
            existing = Trip.objects.filter(uuid=uuid_str).first()
            if existing is not None:
                # Ours already (same-instance restore): nothing to do. Someone
                # else's: a user-supplied archive must never claim it.
                result.inc_skipped("trips")
                continue

        start_date = parse_date(str(row.get("start_date") or "")) if row.get("start_date") else None
        end_date = parse_date(str(row.get("end_date") or "")) if row.get("end_date") else None

        from django.utils import timezone

        if start_date is not None and start_date >= timezone.localdate() and max_upcoming > 0 and upcoming_count >= max_upcoming:
            result.warnings.append(f"Skipped trip '{name}': you already have the maximum of {max_upcoming} upcoming trips.")
            result.inc_skipped("trips")
            continue

        trip = Trip(name=name, description=row.get("description") or None, start_date=start_date, end_date=end_date, creator=profile)
        if uuid_str:
            trip.uuid = uuid_str
        trip.save()
        TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED})
        if start_date is not None and start_date >= timezone.localdate():
            upcoming_count += 1

        # Re-invite exported members: connections only (mirroring
        # TripCreateView - never trust arbitrary identifiers from the
        # archive), capped, always as an invitation the member accepts or
        # ignores themselves.
        member_uuids = [_safe_uuid(value) for value in (row.get("member_uuids") or [])]
        invitable = [member_uuid for member_uuid in member_uuids if member_uuid and member_uuid in connection_uuids]
        remaining = max_members - trip.profiles.count()
        for member_profile in Profile.objects.filter(uuid__in=invitable)[: max(remaining, 0)]:
            if member_profile.pk == profile.pk:
                continue
            _membership, invited = TripMembership.objects.get_or_create(trip=trip, profile=member_profile, defaults={"status": TripMembership.STATUS_INVITED})
            if invited:
                from urbanlens.dashboard.services.trips.trip_membership import notify_added_to_trip as _notify_added_to_trip

                _notify_added_to_trip(profile, member_profile, trip)
        _apply_exported_created(trip, row.get("created"))
        result.inc_created("trips")

    if not_owned:
        result.warnings.append(f"Skipped {not_owned} trip(s) created by someone else - only trips you created can be rebuilt from an export.")


def _import_direct_messages(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    label_uuid_map: dict[str, int],
    report_progress: ProgressReporter | None = None,
) -> None:
    """Restore the user's own SENT plaintext messages into their conversations.

    The narrow slice that can be restored honestly:

    * **Received rows are never imported** - that would let a crafted archive
      fabricate messages "from" a real user (the same forgery
      ``_import_connections`` refuses for incoming friendship rows).
    * **Encrypted rows are never imported** - their ciphertext is sealed to
      the exporting account's key material and the server can't re-wrap what
      it can't read; the ciphertext stays available in the archive itself.
    * Sent plaintext rows are re-created only when the partner exists here,
      isn't muted either way, and ``can_direct_message`` (the same chokepoint
      the composer uses) still permits messaging them.

    Rows are inserted directly - deliberately NOT through
    ``create_direct_message`` - so restoring history never pushes live
    WebSocket events, bell notifications, or text alerts at the partner.
    Exported read state is preserved so old messages don't reappear unread.
    Idempotent per (partner, exported timestamp).
    """
    from django.utils.dateparse import parse_datetime

    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.core.text_limits import MAX_DIRECT_MESSAGE_LENGTH
    from urbanlens.dashboard.services.messaging.direct_messages import can_direct_message

    rows = _read_json(data_dir, "direct_messages.json")
    if not rows:
        return
    total_rows = len(rows)
    not_restorable = 0
    partner_cache: dict[str, Any] = {}

    for idx, row in enumerate(rows, start=1):
        if report_progress:
            report_progress(idx, total_rows)

        partner_uuid = _safe_uuid(row.get("partner_uuid"))
        body = (row.get("body") or "").strip()
        if row.get("direction") != "sent" or row.get("encrypted") or row.get("is_tombstoned") or not body or partner_uuid is None or len(body) > MAX_DIRECT_MESSAGE_LENGTH:
            not_restorable += 1
            result.inc_skipped("direct_messages")
            continue

        if partner_uuid not in partner_cache:
            partner_cache[partner_uuid] = Profile.objects.filter(uuid=partner_uuid).first()
        partner = partner_cache[partner_uuid]
        if partner is None or partner.pk == profile.pk or not can_direct_message(profile, partner) or DirectMessageMute.objects.for_pair(profile, partner).exists() or DirectMessageMute.objects.for_pair(partner, profile).exists():
            not_restorable += 1
            result.inc_skipped("direct_messages")
            continue

        created = parse_datetime(str(row.get("created") or ""))
        if created is None:
            result.inc_skipped("direct_messages")
            continue
        if DirectMessage.objects.filter(sender=profile, recipient=partner, created=created).exists():
            result.inc_skipped("direct_messages")
            continue

        message = DirectMessage.objects.create(
            sender=profile,
            recipient=partner,
            body=body,
            # Preserve exported read state (read timestamps aren't exported,
            # so the deletion moment is approximated by the send moment) - a
            # restored years-old message must not land as "unread" in the
            # partner's badge count.
            read_at=created if row.get("read") else None,
        )
        _apply_exported_created(message, row.get("created"))
        result.inc_created("direct_messages")

    if not_restorable:
        result.warnings.append(
            f"{not_restorable} direct message(s) were archive-only and not restored (received, encrypted, or the partner isn't reachable on this instance). They remain readable in the export file itself.",
        )


# -- Declarative import types ----------------------------------------------------


@dataclass
class ImportContext:
    """Everything one import step needs, so adding a step never widens a signature.

    Attributes:
        profile: The profile being imported into.
        data_dir: Directory holding the extracted archive's data files.
        result: The shared created/skipped/warning tally.
        pin_uuid_map: Archive pin uuid -> local pk, built by the pins step.
        label_uuid_map: Archive label uuid -> local pk, built by the labels step.
        report_progress: Optional throttled (done, count) progress callback.
        scratch: Per-run storage for a step that needs to carry a count or a
            cached lookup from its rows into :meth:`RowImportType.finish`.
            Registered import types are module-level singletons, so a step must
            never keep that state on ``self``.
    """

    profile: Any
    data_dir: str
    result: ImportResult
    pin_uuid_map: dict[str, int]
    label_uuid_map: dict[str, int]
    report_progress: ProgressReporter | None = None
    scratch: dict[str, Any] = field(default_factory=dict)

    def bump(self, key: str, amount: int = 1) -> None:
        """Increment a named counter in :attr:`scratch`.

        Args:
            key: Counter name.
            amount: How much to add.
        """
        self.scratch[key] = self.scratch.get(key, 0) + amount


class ImportType(ABC):
    """One import step, reading a single file from the archive.

    The original steps are plain ``_import_*`` functions listed in
    :data:`_IMPORTERS`. Steps added since subclass this instead: declare a
    key/filename/message, implement :meth:`run`, and append an instance to
    :data:`_REGISTERED_IMPORT_TYPES` plus its key to :data:`_IMPORT_ORDER` (the
    order is a real dependency graph - map annotations need pins, safety
    check-ins need maps - so it stays hand-written).

    Instances are callable with the same signature the legacy importers use, so
    :func:`run_import` dispatches to both identically.

    Attributes:
        key: Manifest/export-type key this step handles.
        filename: File in the archive's data directory, relative to it.
        message: Progress message shown while this step runs.
    """

    key: ClassVar[str]
    filename: ClassVar[str]
    message: ClassVar[str]

    def load(self, data_dir: str) -> Any:
        """Read this step's file from the archive.

        Args:
            data_dir: Directory holding the extracted archive's data files.

        Returns:
            The parsed JSON, or None when the file is absent.
        """
        return _read_json(data_dir, self.filename)

    @abstractmethod
    def run(self, data: Any, ctx: ImportContext) -> None:
        """Apply the loaded archive data to the importing profile.

        Args:
            data: Whatever :meth:`load` returned (never empty/None).
            ctx: The shared import context.
        """

    def __call__(
        self,
        profile: Any,
        data_dir: str,
        result: ImportResult,
        *,
        pin_uuid_map: dict[str, int],
        label_uuid_map: dict[str, int],
        report_progress: ProgressReporter | None = None,
    ) -> None:
        """Run this import step.

        Args:
            profile: The profile being imported into.
            data_dir: Directory holding the extracted archive's data files.
            result: The shared created/skipped/warning tally.
            pin_uuid_map: Archive pin uuid -> local pk.
            label_uuid_map: Archive label uuid -> local pk.
            report_progress: Optional throttled progress callback.
        """
        data = self.load(data_dir)
        if not data:
            return
        self.run(
            data,
            ImportContext(
                profile=profile,
                data_dir=data_dir,
                result=result,
                pin_uuid_map=pin_uuid_map,
                label_uuid_map=label_uuid_map,
                report_progress=report_progress,
            ),
        )


class RowImportType(ImportType):
    """An :class:`ImportType` over a JSON list, handling one row at a time.

    Takes care of the bookkeeping every row-shaped importer repeats: progress
    reporting, the created/skipped tally, discarding non-dict rows from a
    hand-edited archive, and an optional whole-step permission gate.
    """

    def allowed(self, ctx: ImportContext) -> bool:
        """Whether this step may run at all for the importing profile.

        Overridden by steps behind a privacy setting (the same gate
        ``_import_visit_history`` applies before creating any visit).

        Args:
            ctx: The shared import context.

        Returns:
            True to process rows; False to skip the whole step.
        """
        return True

    @abstractmethod
    def import_row(self, row: dict[str, Any], ctx: ImportContext) -> bool:
        """Apply one archive row.

        Args:
            row: The exported row.
            ctx: The shared import context.

        Returns:
            True when a record was created, False when the row was skipped.
        """

    def finish(self, ctx: ImportContext) -> None:
        """Hook for a summary warning once every row has been handled.

        Args:
            ctx: The shared import context.
        """

    def run(self, data: Any, ctx: ImportContext) -> None:
        """Iterate the archive's rows, tallying and reporting as it goes.

        Args:
            data: The parsed JSON list.
            ctx: The shared import context.
        """
        rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        total = len(rows)
        if not total:
            return
        if not self.allowed(ctx):
            ctx.result.inc_skipped(self.key, total)
            return

        for idx, row in enumerate(rows, start=1):
            if ctx.report_progress:
                ctx.report_progress(idx, total)
            if self.import_row(row, ctx):
                ctx.result.inc_created(self.key)
            else:
                ctx.result.inc_skipped(self.key)
        self.finish(ctx)


def _decimal_or_none(value: Any) -> Any:
    """Parse an exported decimal string, returning None when it isn't one.

    Args:
        value: The raw exported value.

    Returns:
        A ``Decimal``, or None when absent/unparseable.
    """
    from decimal import Decimal, InvalidOperation

    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _connection_uuids(ctx: ImportContext) -> set[str]:
    """Return (and cache) the uuids of the importing profile's connections.

    Args:
        ctx: The shared import context.

    Returns:
        Set of connected profile uuids as strings.
    """
    cached = ctx.scratch.get("connection_uuids")
    if cached is None:
        from urbanlens.dashboard.services.social.connections import get_connections

        cached = {str(connection.uuid) for connection in get_connections(ctx.profile)}
        ctx.scratch["connection_uuids"] = cached
    return cached


class ProfileImport(ImportType):
    """Restore the profile's own free-text content and contact handles.

    Identity is deliberately NOT restored. ``username``, ``email``,
    ``first_name``, ``last_name`` and ``date_joined`` all live on ``auth.User``
    and identify the *account*, not its content: an archive is routinely
    imported into a different account (that is the whole point of a portable
    export), and letting one overwrite the destination account's login identity
    would be an account-takeover primitive rather than a restore. The fields
    below are the ones the user typed into their own profile page and can
    already see sitting in ``profile.json``.

    ``social_links`` and ``secondary_emails`` ride along in the same file
    (``_export_profile`` writes them there) and are restored here too - deduped
    against what the profile already has, and always unverified: verification is
    a claim on an address that only this instance's confirmation flow can grant,
    so honoring an exported ``is_verified`` would let a hand-edited archive
    claim someone else's address.
    """

    key = "profile"
    filename = "profile.json"
    message = "Importing profile..."

    #: Free-text Profile columns copied across verbatim.
    text_fields: ClassVar[tuple[str, ...]] = ("bio", "area")

    #: Profile date columns, applied only when they parse.
    date_fields: ClassVar[tuple[str, ...]] = ("birth_date", "started_exploring")

    #: The contact block - encrypted at rest, plain strings in the archive.
    contact_fields: ClassVar[tuple[str, ...]] = (
        "phone_number",
        "signal_username",
        "discord_username",
        "whatsapp_number",
        "telegram_username",
        "matrix_handle",
    )

    def run(self, data: Any, ctx: ImportContext) -> None:
        """Apply the exported profile content to the importing profile.

        Args:
            data: The parsed ``profile.json`` object.
            ctx: The shared import context.
        """
        from django.utils.dateparse import parse_date

        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.core.text_limits import MAX_PROFILE_BIO_LENGTH

        if not isinstance(data, dict):
            return

        update_fields: dict[str, Any] = {}
        for name in self.text_fields:
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                # .update() bypasses field validators, so the bio ceiling is
                # enforced here rather than relying on full_clean().
                update_fields[name] = value[:MAX_PROFILE_BIO_LENGTH] if name == "bio" else value

        for name in self.date_fields:
            raw = data.get(name)
            parsed = parse_date(str(raw)) if raw else None
            if parsed is not None:
                update_fields[name] = parsed

        contact = data.get("contact")
        if isinstance(contact, dict):
            for name in self.contact_fields:
                value = contact.get(name)
                if isinstance(value, str) and value.strip():
                    update_fields[name] = value

        if update_fields:
            Profile.objects.filter(pk=ctx.profile.pk).update(**update_fields)
            ctx.result.inc_created(self.key)
        else:
            ctx.result.inc_skipped(self.key)

        self._import_social_links(data.get("social_links"), ctx)
        self._import_secondary_emails(data.get("secondary_emails"), ctx)

    def _import_social_links(self, rows: Any, ctx: ImportContext) -> None:
        """Restore one link per platform, never overwriting one already set.

        Args:
            rows: The exported ``social_links`` list.
            ctx: The shared import context.
        """
        from urbanlens.dashboard.models.social_link.model import SocialLink

        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            platform = str(row.get("platform") or "").strip()[:30]
            handle = str(row.get("handle") or "").strip()[:500]
            if not platform or not handle:
                ctx.result.inc_skipped("social_links")
                continue
            # get_or_create on the model's own (profile, platform) uniqueness:
            # a link the user already set here wins over the archive's copy.
            _link, created = SocialLink.objects.get_or_create(profile=ctx.profile, platform=platform, defaults={"handle": handle})
            if created:
                ctx.result.inc_created("social_links")
            else:
                ctx.result.inc_skipped("social_links")

    def _import_secondary_emails(self, rows: Any, ctx: ImportContext) -> None:
        """Restore additional addresses as unverified rows.

        Args:
            rows: The exported ``secondary_emails`` list.
            ctx: The shared import context.
        """
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email

        from urbanlens.dashboard.models.profile.email import ProfileEmail
        from urbanlens.dashboard.services.auth.email_normalization import normalize_email

        if not isinstance(rows, list):
            return
        needs_reverification = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            email = str(row.get("email") or "").strip()
            try:
                validate_email(email)
            except ValidationError:
                ctx.result.inc_skipped("secondary_emails")
                continue

            normalized = normalize_email(email)
            if ProfileEmail.objects.filter(profile=ctx.profile, normalized_email=normalized).exists():
                ctx.result.inc_skipped("secondary_emails")
                continue

            ProfileEmail.objects.create(profile=ctx.profile, email=email, is_verified=False)
            ctx.result.inc_created("secondary_emails")
            if row.get("is_verified"):
                needs_reverification += 1

        if needs_reverification:
            ctx.result.warnings.append(
                f"{needs_reverification} secondary email address(es) were restored unverified - confirm them again from your profile to use them.",
            )


class SafetyCheckinsImport(RowImportType):
    """Restore safety check-ins as historical records, never as live plans.

    A restored check-in is always given a *terminal* status. An exported
    ``scheduled``/``awaiting_checkin``/``overdue`` row still has a future
    ``checkin_by``, so importing it as-is would re-arm a plan the user is not
    actually on and eventually email their emergency contacts about a trip that
    never happened - an import must not be able to send mail on the user's
    behalf. Non-terminal rows therefore land as ``cancelled``, with a warning
    saying so; a check-in that had already concluded keeps the status it had.

    ``notify_community_wiki`` is likewise forced off, so a restore can never
    post to a community wiki. Contact rows are re-created from the snapshot the
    user typed, but never their ``token`` (a fresh unguessable one is generated,
    and the old one was never exported), and a contact is only re-linked to a
    real account when that account is one of the importer's own connections -
    the "requests, not facts" rule ``_import_trips`` applies to trip members.
    Only messages the owner themselves wrote are restored: re-creating a
    contact's reply would fabricate words attributed to that person.
    """

    key = "safety_checkins"
    filename = "safety_checkins.json"
    message = "Importing safety check-ins..."

    def import_row(self, row: dict[str, Any], ctx: ImportContext) -> bool:
        """Restore one check-in with its contacts and the owner's own messages.

        Args:
            row: The exported check-in row.
            ctx: The shared import context.

        Returns:
            True when a check-in was created.
        """
        from datetime import timedelta

        from django.utils.dateparse import parse_datetime

        from urbanlens.dashboard.models.safety.model import DEFAULT_GRACE_PERIOD, SafetyCheckin, SafetyCheckinStatus

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and SafetyCheckin.objects.filter(uuid=uuid_str, profile=ctx.profile).exists():
            return False

        title = str(row.get("title") or "").strip()[:200]
        checkin_by = parse_datetime(str(row.get("checkin_by") or ""))
        if not title or checkin_by is None:
            return False

        # Content-level backstop, same shape as _import_comments': when the
        # archive's uuid is already taken by another row the uuid check above
        # can never fire, so an identical (title, checkin_by) pair is the same
        # check-in.
        if SafetyCheckin.objects.filter(profile=ctx.profile, title=title, checkin_by=checkin_by).exists():
            return False

        status = row.get("status")
        if status not in SafetyCheckinStatus.resolved_statuses():
            status = SafetyCheckinStatus.CANCELLED
            ctx.bump("disarmed")

        grace_seconds = row.get("grace_period_seconds")
        grace_period = timedelta(seconds=float(grace_seconds)) if isinstance(grace_seconds, int | float) else DEFAULT_GRACE_PERIOD

        exported_created = parse_datetime(str(row.get("created") or ""))
        resolved_at = parse_datetime(str(row.get("resolved_at") or "")) or exported_created or checkin_by

        checkin = SafetyCheckin(
            profile=ctx.profile,
            title=title,
            plan_details=str(row.get("plan_details") or ""),
            contact_message=str(row.get("contact_message") or ""),
            checkin_by=checkin_by,
            grace_period=grace_period,
            status=status,
            destination_latitude=_decimal_or_none(row.get("destination_latitude")),
            destination_longitude=_decimal_or_none(row.get("destination_longitude")),
            # Never re-post to a community wiki from a restore.
            notify_community_wiki=False,
            escalated_at=parse_datetime(str(row.get("escalated_at") or "")),
            resolved_at=resolved_at,
            resolved_by_label=str(row.get("resolved_by_label") or "")[:150],
            trip=self._resolve_trip(row.get("trip_uuid"), ctx),
            markup_map=self._resolve_map(row.get("markup_map_uuid"), ctx),
        )
        if uuid_str and not SafetyCheckin.objects.filter(uuid=uuid_str).exists():
            checkin.uuid = uuid_str
        checkin.save()

        attached = [self._resolve_map(value, ctx) for value in (row.get("attached_markup_map_uuids") or [])]
        resolved_attachments = [markup_map for markup_map in attached if markup_map is not None]
        if resolved_attachments:
            checkin.markup_maps.add(*resolved_attachments)

        self._import_contacts(checkin, row.get("contacts"), ctx)
        self._import_messages(checkin, row.get("messages"), ctx)
        _apply_exported_created(checkin, row.get("created"))
        return True

    def _resolve_trip(self, trip_uuid: Any, ctx: ImportContext) -> Any:
        """Resolve an exported trip uuid to one of the importer's own trips.

        Args:
            trip_uuid: The exported uuid, if any.
            ctx: The shared import context.

        Returns:
            The Trip, or None when it isn't the importer's.
        """
        from urbanlens.dashboard.models.trips.model import Trip

        parsed = _safe_uuid(trip_uuid)
        if parsed is None:
            return None
        return Trip.objects.filter(uuid=parsed, profiles=ctx.profile).first()

    def _resolve_map(self, map_uuid: Any, ctx: ImportContext) -> Any:
        """Resolve an exported markup-map uuid to one of the importer's own maps.

        The map annotations step runs first and preserves archive uuids where
        they were free, so a same-archive restore re-links; a uuid pointing at
        someone else's map resolves to nothing rather than borrowing it.

        Args:
            map_uuid: The exported uuid, if any.
            ctx: The shared import context.

        Returns:
            The MarkupMap, or None.
        """
        from urbanlens.dashboard.models.markup.model import MarkupMap

        parsed = _safe_uuid(map_uuid)
        if parsed is None:
            return None
        return MarkupMap.objects.filter(uuid=parsed, profile=ctx.profile).first()

    def _import_contacts(self, checkin: Any, rows: Any, ctx: ImportContext) -> None:
        """Re-create the check-in's emergency contact snapshots.

        Args:
            checkin: The freshly created check-in.
            rows: The exported ``contacts`` list.
            ctx: The shared import context.
        """
        from django.utils.dateparse import parse_datetime

        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.safety.model import SafetyCheckinContact

        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            contact_uuid = _safe_uuid(row.get("contact_profile_uuid"))
            contact_profile = None
            if contact_uuid and contact_uuid in _connection_uuids(ctx):
                contact_profile = Profile.objects.filter(uuid=contact_uuid).first()

            email = str(row.get("email") or "").strip() or None
            if contact_profile is None and not email:
                ctx.result.inc_skipped("safety_contacts")
                continue

            SafetyCheckinContact.objects.create(
                checkin=checkin,
                name=str(row.get("name") or "")[:150],
                # The model's CheckConstraint is an XOR: a row identifies its
                # contact by account or by address, never both.
                email=None if contact_profile is not None else email,
                contact_profile=contact_profile,
                notified_at=parse_datetime(str(row.get("notified_at") or "")),
                found_safe_at=parse_datetime(str(row.get("found_safe_at") or "")),
            )
            ctx.result.inc_created("safety_contacts")

    def _import_messages(self, checkin: Any, rows: Any, ctx: ImportContext) -> None:
        """Restore only the messages the check-in's owner wrote themselves.

        Args:
            checkin: The freshly created check-in.
            rows: The exported ``messages`` list.
            ctx: The shared import context.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinMessage

        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            body = str(row.get("body") or "").strip()
            if not body:
                continue
            if row.get("sender") != "owner":
                ctx.bump("foreign_messages")
                ctx.result.inc_skipped("safety_messages")
                continue
            message = SafetyCheckinMessage.objects.create(checkin=checkin, sender_profile=ctx.profile, body=body)
            _apply_exported_created(message, row.get("created"))
            ctx.result.inc_created("safety_messages")

    def finish(self, ctx: ImportContext) -> None:
        """Explain the two ways a restored check-in differs from the exported one.

        Args:
            ctx: The shared import context.
        """
        disarmed = ctx.scratch.get("disarmed", 0)
        if disarmed:
            ctx.result.warnings.append(
                f"{disarmed} safety check-in(s) were still active when exported and were restored as cancelled - importing them live would have re-armed alerts to your emergency contacts.",
            )
        foreign = ctx.scratch.get("foreign_messages", 0)
        if foreign:
            ctx.result.warnings.append(
                f"{foreign} safety check-in message(s) written by your contacts were not restored. They remain readable in the export file itself.",
            )


class MapAnnotationsImport(ImportType):
    """Restore markup maps, their annotations, and georeferenced image overlays.

    Only annotations the importer can own outright come back: standalone maps
    and everything drawn on them, plus markup and overlays attached to the
    importer's OWN pins (resolved through ``_resolve_import_target``, the same
    uuid-only matching comments and photos use). Wiki-scoped annotations are
    shared community data on someone else's page - exported so the user keeps a
    copy of what they drew, but skipped here rather than written back into a
    community map from a user-supplied file.

    An overlay's image re-enters storage through the same quota and file-size
    checks a fresh upload gets, exactly as ``_import_photos`` does; one that
    doesn't fit falls back to its external ``image_url`` when it had one, and is
    otherwise skipped rather than restored as an overlay with nothing to draw.
    """

    key = "map_annotations"
    filename = "map_annotations.json"
    message = "Importing map annotations..."

    def run(self, data: Any, ctx: ImportContext) -> None:
        """Restore the three annotation sections in dependency order.

        Args:
            data: The parsed ``map_annotations.json`` object.
            ctx: The shared import context.
        """
        if not isinstance(data, dict):
            return

        maps = [row for row in (data.get("maps") or []) if isinstance(row, dict)]
        markup = [row for row in (data.get("markup") or []) if isinstance(row, dict)]
        overlays = [row for row in (data.get("overlays") or []) if isinstance(row, dict)]
        total = len(maps) + len(markup) + len(overlays)
        done = 0

        for row in maps:
            done += 1
            if ctx.report_progress:
                ctx.report_progress(done, total)
            self._import_map(row, ctx)

        for row in markup:
            done += 1
            if ctx.report_progress:
                ctx.report_progress(done, total)
            self._import_standalone_markup(row, ctx)

        for row in overlays:
            done += 1
            if ctx.report_progress:
                ctx.report_progress(done, total)
            self._import_overlay(row, ctx)

        unattachable = ctx.scratch.get("unattachable", 0)
        if unattachable:
            ctx.result.warnings.append(
                f"Skipped {unattachable} map annotation(s) drawn on a community wiki or on a pin that could not be matched on this instance.",
            )

    def _import_map(self, row: dict[str, Any], ctx: ImportContext) -> None:
        """Restore one standalone markup map and the items drawn on it.

        Args:
            row: The exported map row.
            ctx: The shared import context.
        """
        from urbanlens.dashboard.models.markup.model import MarkupMap

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and MarkupMap.objects.filter(uuid=uuid_str, profile=ctx.profile).exists():
            ctx.result.inc_skipped("map_annotations")
            return

        markup_map = MarkupMap(
            profile=ctx.profile,
            title=str(row.get("title") or "")[:200],
            center_latitude=_float_or_none(row.get("center_latitude")),
            center_longitude=_float_or_none(row.get("center_longitude")),
            zoom=_float_or_none(row.get("zoom")),
            show_borders=bool(row.get("show_borders")),
            pin_id=ctx.pin_uuid_map.get(_safe_uuid(row.get("pin_uuid")) or ""),
        )
        markup_map.layer_mode = _valid_layer_mode(row.get("layer_mode"))
        if uuid_str and not MarkupMap.objects.filter(uuid=uuid_str).exists():
            markup_map.uuid = uuid_str
        markup_map.save()
        _apply_exported_created(markup_map, row.get("created"))
        ctx.result.inc_created("map_annotations")

        for item_row in row.get("items") or []:
            if isinstance(item_row, dict) and _build_markup_item(item_row, ctx.profile, parent_map=markup_map) is not None:
                ctx.result.inc_created("map_annotation_items")

    def _import_standalone_markup(self, row: dict[str, Any], ctx: ImportContext) -> None:
        """Restore one annotation drawn directly on the importer's own pin.

        Args:
            row: The exported markup row.
            ctx: The shared import context.
        """
        from urbanlens.dashboard.models.markup.model import PinMarkup

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and PinMarkup.objects.filter(uuid=uuid_str, profile=ctx.profile).exists():
            ctx.result.inc_skipped("map_annotation_items")
            return

        pin_pk, _wiki, _resolved = _resolve_import_target(ctx.profile, row, ctx.pin_uuid_map)
        if pin_pk is None:
            ctx.bump("unattachable")
            ctx.result.inc_skipped("map_annotation_items")
            return

        if _build_markup_item(row, ctx.profile, parent_pin_id=pin_pk) is None:
            ctx.result.inc_skipped("map_annotation_items")
        else:
            ctx.result.inc_created("map_annotation_items")

    def _import_overlay(self, row: dict[str, Any], ctx: ImportContext) -> None:
        """Restore one georeferenced image overlay onto the importer's own pin.

        Args:
            row: The exported overlay row.
            ctx: The shared import context.
        """
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
        from urbanlens.dashboard.services.media.previews import is_web_safe
        from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, ensure_public_http_url

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and MapImageOverlay.objects.filter(uuid=uuid_str, profile=ctx.profile).exists():
            ctx.result.inc_skipped("map_overlays")
            return

        pin_pk, _wiki, _resolved = _resolve_import_target(ctx.profile, row, ctx.pin_uuid_map)
        if pin_pk is None:
            ctx.bump("unattachable")
            ctx.result.inc_skipped("map_overlays")
            return

        # An imported image_url is rendered client-side as an <img src>, so it
        # gets the same ensure_public_http_url/is_web_safe gate a live form POST
        # gets - an import file is just another untrusted source.
        #
        # It does NOT match the live path any more: that one now downloads a
        # pasted url (controllers.map_overlays._image_from_request) so the
        # column can only hold our own MEDIA_URL, because a stored foreign url
        # reports every viewer's IP and User-Agent back to whoever supplied it.
        # Import still stores the url as-is - it has no download step, and
        # these overlays are pin-scoped, so the only viewer is the importer
        # themselves. Give this the same treatment if import ever grows a
        # download step, or if wiki-scoped overlays become importable.
        image_url = str(row.get("image_url") or "")[:1000]
        if image_url:
            try:
                ensure_public_http_url(image_url, max_length=1000)
            except UnsafeUrlError:
                image_url = ""
            else:
                if not is_web_safe(image_url):
                    image_url = ""

        overlay = MapImageOverlay(
            profile=ctx.profile,
            parent_pin_id=pin_pk,
            name=str(row.get("name") or "")[:100],
            image_url=image_url,
            opacity=_bounded_int(row.get("opacity"), default=70),
            order=_bounded_int(row.get("order"), default=0, low=0, high=10_000),
            default_visible=bool(row.get("default_visible", True)),
            locked=bool(row.get("locked")),
        )
        try:
            overlay.set_corners([[float(lat), float(lng)] for lat, lng in (row.get("corners") or [])])
        except (TypeError, ValueError):
            ctx.result.inc_skipped("map_overlays")
            return

        overlay.image = self._restore_overlay_image(row, ctx)
        if overlay.image is None and not overlay.image_url:
            ctx.bump("imageless_overlays")
            ctx.result.inc_skipped("map_overlays")
            return

        if uuid_str and not MapImageOverlay.objects.filter(uuid=uuid_str).exists():
            overlay.uuid = uuid_str
        overlay.save()
        _apply_exported_created(overlay, row.get("created"))
        ctx.result.inc_created("map_overlays")

    def _restore_overlay_image(self, row: dict[str, Any], ctx: ImportContext) -> Any:
        """Re-upload an overlay's archived image file, honoring the storage quota.

        Args:
            row: The exported overlay row.
            ctx: The shared import context.

        Returns:
            The created Image, or None when the archive had no usable file.
        """
        from django.core.files import File

        from urbanlens.dashboard.models.images.model import Image, MediaKind
        from urbanlens.dashboard.services.media.storage import file_size_error_for_upload, per_profile_upload_lock, quota_error_for_upload

        # basename(): the archive is user-supplied, so its filenames are
        # untrusted input (same guard _import_photos applies).
        filename = os.path.basename(str(row.get("filename") or ""))
        if not filename:
            return None
        src_path = os.path.join(ctx.data_dir, MapAnnotationsExportDirName, filename)
        if not os.path.isfile(src_path):
            return None

        size = os.path.getsize(src_path)
        with per_profile_upload_lock(ctx.profile):
            if file_size_error_for_upload(size) or quota_error_for_upload(ctx.profile, size):
                ctx.result.warnings.append("A map overlay image was skipped because it would exceed your storage quota or the maximum upload size.")
                return None

            image = Image(profile=ctx.profile, media_type=MediaKind.PHOTO, file_size=size)
            with open(src_path, "rb") as fh:
                image.image.save(filename, File(fh), save=True)
        return image


#: Name of the archive subdirectory holding overlay image files. Mirrors
#: ``export.MapAnnotationsExport.files_dir_name``; kept as a literal here so the
#: importer never imports the export module just to read one constant.
MapAnnotationsExportDirName = "map_annotations"


def _float_or_none(value: Any) -> float | None:
    """Return ``value`` as a float, or None when it isn't numeric.

    Args:
        value: The raw exported value.

    Returns:
        A float, or None.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded_int(value: Any, *, default: int, low: int = 0, high: int = 100) -> int:
    """Clamp an exported integer into a valid range, falling back to a default.

    Args:
        value: The raw exported value.
        default: Value used when ``value`` isn't an integer.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        An integer within ``[low, high]``.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _valid_layer_mode(value: Any) -> str:
    """Return an exported base-layer choice, or the model default.

    Args:
        value: The raw exported ``layer_mode``.

    Returns:
        A valid ``MapLayerMode`` value.
    """
    from urbanlens.dashboard.models.markup.meta import normalize_layer_mode

    return normalize_layer_mode(value)


def _build_markup_item(row: dict[str, Any], profile: Any, *, parent_map: Any = None, parent_pin_id: int | None = None) -> Any:
    """Create one PinMarkup from an exported annotation row.

    ``geometry`` is stored as JSON and rendered client-side, so it is only
    accepted as an object; colours are re-sanitized by ``PinMarkup.save()``
    itself, which is why they are passed through here rather than re-validated.

    Args:
        row: The exported annotation row.
        profile: The importing profile (always the author of the new item).
        parent_map: The MarkupMap this item belongs to, if map-scoped.
        parent_pin_id: The Pin pk this item belongs to, if pin-scoped.

    Returns:
        The created PinMarkup, or None when the row was unusable.
    """
    from urbanlens.dashboard.models.markup.meta import MarkupType, SecurityIndicatorType
    from urbanlens.dashboard.models.markup.model import PinMarkup

    markup_type = row.get("markup_type")
    geometry = row.get("geometry")
    if markup_type not in MarkupType.values or not isinstance(geometry, dict):
        return None

    security_indicator = row.get("security_indicator")
    item = PinMarkup(
        profile=profile,
        parent_map=parent_map,
        parent_pin_id=parent_pin_id,
        markup_type=markup_type,
        geometry=geometry,
        label=str(row.get("label") or ""),
        color=str(row.get("color") or "#e53e3e"),
        stroke_width=_bounded_int(row.get("stroke_width"), default=3, low=1, high=200),
        border_color=str(row.get("border_color") or ""),
        fill_opacity=_bounded_int(row.get("fill_opacity"), default=87),
        border_opacity=_bounded_int(row.get("border_opacity"), default=100),
        security_indicator=security_indicator if security_indicator in SecurityIndicatorType.values else "",
    )
    uuid_str = _safe_uuid(row.get("uuid"))
    if uuid_str and not PinMarkup.objects.filter(uuid=uuid_str).exists():
        item.uuid = uuid_str
    item.save()
    _apply_exported_created(item, row.get("created"))
    return item


class SavedFiltersImport(RowImportType):
    """Restore the user's saved main-map filters.

    ``criteria`` is copied back verbatim, the way ``_import_pin_lists`` copies a
    smart list's ``smart_filter``: it is the search layer's own normalized
    payload, and a criterion naming a label that doesn't exist here simply
    matches nothing when the filter is next replayed. Deduped by uuid and then
    by the model's own ``(profile, name)`` uniqueness.
    """

    key = "saved_filters"
    filename = "saved_filters.json"
    message = "Importing saved filters..."

    def import_row(self, row: dict[str, Any], ctx: ImportContext) -> bool:
        """Restore one saved filter.

        Args:
            row: The exported filter row.
            ctx: The shared import context.

        Returns:
            True when a filter was created.
        """
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and SavedFilter.objects.filter(uuid=uuid_str, profile=ctx.profile).exists():
            return False

        name = str(row.get("name") or "").strip()[:100]
        if not name or SavedFilter.objects.filter(profile=ctx.profile, name=name).exists():
            return False

        criteria = row.get("criteria")
        saved_filter = SavedFilter(
            profile=ctx.profile,
            name=name,
            icon=str(row.get("icon") or "bookmark")[:64],
            color=str(row.get("color") or "")[:20],
            opacity=_bounded_int(row.get("opacity"), default=100),
            criteria=criteria if isinstance(criteria, dict) else {},
            order=_bounded_int(row.get("order"), default=0, low=0, high=10_000),
        )
        if uuid_str and not SavedFilter.objects.filter(uuid=uuid_str).exists():
            saved_filter.uuid = uuid_str
        saved_filter.save()
        _apply_exported_created(saved_filter, row.get("created"))
        return True


class RoutesImport(RowImportType):
    """Restore recorded GPS tracks and planned routes.

    Gated on the same ``track_routes`` preference the GPX/Takeout importers
    check (``services.visits.visits.route_import_allowed``) - a user who turned
    route tracking off should not get routes back by way of an archive, exactly
    as ``_import_visit_history`` refuses to create visits when visit logging is
    off. Deduped by uuid, then by the exported creation timestamp, which
    ``_apply_exported_created`` preserves so a re-import recognises its own work.
    """

    key = "routes"
    filename = "routes.json"
    message = "Importing routes..."

    def allowed(self, ctx: ImportContext) -> bool:
        """Whether route data may be created for the importing profile.

        Args:
            ctx: The shared import context.

        Returns:
            True when the profile has route tracking enabled.
        """
        from urbanlens.dashboard.services.visits.visits import route_import_allowed

        return route_import_allowed(ctx.profile)

    def import_row(self, row: dict[str, Any], ctx: ImportContext) -> bool:
        """Restore one route.

        Args:
            row: The exported route row.
            ctx: The shared import context.

        Returns:
            True when a route was created.
        """
        from django.utils.dateparse import parse_datetime

        from urbanlens.dashboard.models.routes.model import Route, RouteSource

        uuid_str = _safe_uuid(row.get("uuid"))
        if uuid_str and Route.objects.filter(uuid=uuid_str, profile=ctx.profile).exists():
            return False

        path = _linestring_from_geojson(row.get("path"))
        if path is None:
            return False

        exported_created = parse_datetime(str(row.get("created") or ""))
        if exported_created is not None and Route.objects.filter(profile=ctx.profile, created=exported_created).exists():
            return False

        source = row.get("source")
        route = Route(
            profile=ctx.profile,
            name=str(row.get("name") or "")[:255],
            source=source if source in RouteSource.values else RouteSource.GPX_TRACK,
            source_filename=str(row.get("source_filename") or "")[:255],
            path=path,
            raw_point_count=_bounded_int(row.get("raw_point_count"), default=0, low=0, high=10_000_000),
            simplified_point_count=_bounded_int(row.get("simplified_point_count"), default=0, low=0, high=10_000_000),
            distance_meters=_float_or_none(row.get("distance_meters")) or 0.0,
            elevation_gain_meters=_float_or_none(row.get("elevation_gain_meters")),
            elevation_loss_meters=_float_or_none(row.get("elevation_loss_meters")),
            started_at=parse_datetime(str(row.get("started_at") or "")),
            ended_at=parse_datetime(str(row.get("ended_at") or "")),
        )
        if uuid_str and not Route.objects.filter(uuid=uuid_str).exists():
            route.uuid = uuid_str
        route.save()
        _apply_exported_created(route, row.get("created"))
        return True


def _linestring_from_geojson(value: Any) -> Any:
    """Parse an exported GeoJSON path into a WGS-84 LineString.

    Args:
        value: The exported ``path`` object.

    Returns:
        A ``LineString`` in SRID 4326, or None when the payload isn't one.
    """
    from django.contrib.gis.geos import GEOSGeometry
    from django.contrib.gis.geos.error import GEOSException

    if not isinstance(value, dict):
        return None
    try:
        geometry = GEOSGeometry(json.dumps(value), srid=4326)
    except (GEOSException, ValueError, TypeError):
        return None
    if geometry.geom_type != "LineString" or geometry.num_coords < 2:
        return None
    return geometry


# -- Dispatch table -------------------------------------------------------------

#: Every registry-backed import step. Adding one is: write an
#: :class:`ImportType` subclass, append an instance here, and place its key in
#: :data:`_IMPORT_ORDER`.
_REGISTERED_IMPORT_TYPES: tuple[ImportType, ...] = (
    ProfileImport(),
    MapAnnotationsImport(),
    SafetyCheckinsImport(),
    SavedFiltersImport(),
    RoutesImport(),
)

_REGISTERED_IMPORTERS: dict[str, ImportType] = {import_type.key: import_type for import_type in _REGISTERED_IMPORT_TYPES}

#: Run order. Hand-written because it encodes real dependencies: labels before
#: pins, pins before anything that attaches to one, markup maps before the
#: safety check-ins that reference them, and settings last so an imported
#: preference can't gate an earlier step.
_IMPORT_ORDER = [
    "profile",
    "labels",
    "pins",
    "custom_fields",
    "pin_lists",
    "visit_history",
    "comments",
    "photos",
    "map_annotations",
    "trips",
    "safety_checkins",
    "saved_filters",
    "routes",
    "direct_messages",
    "connections",
    "settings",
]

_IMPORTERS: dict[str, Any] = {
    "labels": _import_labels,
    "pins": _import_pins,
    "custom_fields": _import_custom_fields,
    "pin_lists": _import_pin_lists,
    "visit_history": _import_visit_history,
    "comments": _import_comments,
    "photos": _import_photos,
    "trips": _import_trips,
    "direct_messages": _import_direct_messages,
    "connections": _import_connections,
    "settings": _import_settings,
    **_REGISTERED_IMPORTERS,
}

_STEP_MESSAGES = {
    "labels": "Importing labels...",
    "pins": "Importing pins and locations...",
    "custom_fields": "Importing custom fields...",
    "pin_lists": "Importing lists...",
    "visit_history": "Importing visit history...",
    "comments": "Importing notes and comments...",
    "photos": "Importing photos...",
    "trips": "Importing trips...",
    "direct_messages": "Restoring messages...",
    "connections": "Importing connections...",
    "settings": "Applying settings...",
    **{key: import_type.message for key, import_type in _REGISTERED_IMPORTERS.items()},
}
