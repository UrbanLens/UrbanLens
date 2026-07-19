"""Data import service - parse and apply a UrbanLens export archive."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import os
import shutil
from typing import Any
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
    from urbanlens.dashboard.services.celery import safely_enqueue_task
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
    from urbanlens.dashboard.services.storage import get_quota_bytes

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
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise _ImportValidationError("Archive contains too many files.")
        if sum(member.file_size for member in members) > _extraction_size_ceiling(profile):
            raise _ImportValidationError("Archive is too large to import.")
        # Guard against zip-slip path traversal. The separator is part of the
        # comparison on purpose: a bare prefix check would accept an entry
        # escaping into a SIBLING directory whose name merely starts with the
        # extract dir's (e.g. ".../job1" matching ".../job1evil/...").
        for member in members:
            dest = os.path.realpath(os.path.join(extract_root, member.filename))
            if dest != extract_root and not dest.startswith(extract_root + os.sep):
                raise _ImportValidationError("Archive contains invalid file paths.")
        zf.extractall(extract_root)

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
    from urbanlens.dashboard.services.content_sniffing import content_type_mismatch_error, guess_media_kind_from_extension
    from urbanlens.dashboard.services.malware_scan import MalwareScanUnavailableError, malware_error_for_upload

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
            existing = Label.objects.filter(uuid=label_uuid, profile=profile).first() or Label.objects.filter(profile=profile, name=name, kind=kind).first()
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
            existing = Label.objects.filter(profile__isnull=True, name=name, kind=kind).first()
            if existing:
                label_uuid_map[uuid_str] = existing.pk
                result.inc_skipped("labels")
            else:
                # Re-create as a user-owned label (global doesn't exist on this instance).
                user_existing = Label.objects.filter(profile=profile, name=name, kind=kind).first()
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

        rating = row.get("rating")
        if isinstance(rating, int) and 0 <= rating <= 5:
            from urbanlens.dashboard.models.reviews.model import Review

            Review.objects.create(profile=profile, pin=pin, rating=rating)

        article_data = row.get("article") or {}
        content = article_data.get("content")
        if content:
            from urbanlens.dashboard.services.articles import save_article

            save_article(editor=profile, content=content, edit_summary="Imported", pin=pin)


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
    from urbanlens.dashboard.services.visits import visit_logging_allowed

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
    "external_apis_enabled",
    "name_source_priority",
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
            from urbanlens.dashboard.services.geo import parse_multipolygon_geojson

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


# -- Dispatch table -------------------------------------------------------------

_IMPORT_ORDER = ["labels", "pins", "custom_fields", "pin_lists", "visit_history", "connections", "settings"]

_IMPORTERS: dict[str, Any] = {
    "labels": _import_labels,
    "pins": _import_pins,
    "custom_fields": _import_custom_fields,
    "pin_lists": _import_pin_lists,
    "visit_history": _import_visit_history,
    "connections": _import_connections,
    "settings": _import_settings,
}

_STEP_MESSAGES = {
    "labels": "Importing labels...",
    "pins": "Importing pins and locations...",
    "custom_fields": "Importing custom fields...",
    "pin_lists": "Importing lists...",
    "visit_history": "Importing visit history...",
    "connections": "Importing connections...",
    "settings": "Applying settings...",
}
