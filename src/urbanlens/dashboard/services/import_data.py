"""Data import service — parse and apply a UrbanLens export archive."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import shutil
from typing import Any
import zipfile

from django.core.cache import cache

logger = logging.getLogger(__name__)

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
        job_status.write("running", 5, "Validating archive…")
        data_dir = _extract_and_validate(zip_path, extract_dir, job_id)

        manifest = _read_json(data_dir, "manifest.json") or {}
        contents: list[str] = manifest.get("contents", [])

        steps = [k for k in _IMPORT_ORDER if k in contents]
        total = len(steps) + 1

        # Cache of UUID→PK mappings built as we go, needed for cross-references.
        pin_uuid_map: dict[str, int] = {}
        badge_uuid_map: dict[str, int] = {}

        for i, key in enumerate(steps):
            progress = 10 + int((i / total) * 80)
            job_status.write("running", progress, _STEP_MESSAGES.get(key, f"Importing {key}…"))

            importer = _IMPORTERS.get(key)
            if importer is None:
                continue
            importer(profile, data_dir, result, pin_uuid_map=pin_uuid_map, badge_uuid_map=badge_uuid_map)

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
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        schedule_import_cleanup(os.path.dirname(zip_path), job_status)


# ── Validation ────────────────────────────────────────────────────────────────


class _ImportValidationError(Exception):
    pass


def _extract_and_validate(zip_path: str, extract_dir: str, job_id: str) -> str:
    """Extract the ZIP and return the path to the data directory inside it."""
    if not os.path.exists(zip_path):
        raise _ImportValidationError("Uploaded file not found. Please try again.")

    if not zipfile.is_zipfile(zip_path):
        raise _ImportValidationError("The uploaded file is not a valid ZIP archive.")

    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Guard against zip-slip path traversal.
        for member in zf.namelist():
            dest = os.path.realpath(os.path.join(extract_dir, member))
            if not dest.startswith(os.path.realpath(extract_dir)):
                raise _ImportValidationError("Archive contains invalid file paths.")
        zf.extractall(extract_dir)

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


# ── Individual importers ───────────────────────────────────────────────────────


def _import_badges(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    badge_uuid_map: dict[str, int],
) -> None:
    """Import user-owned badge definitions. Global badges are matched by name."""
    from urbanlens.dashboard.models.badges.model import Badge

    rows = _read_json(data_dir, "badges.json")
    if not rows:
        return

    for row in rows:
        uuid_str = row.get("uuid", "")
        name = row.get("name", "").strip()
        if not name:
            result.inc_skipped("badges")
            continue

        is_user_badge = row.get("is_user_badge", True)

        if is_user_badge:
            # Match by UUID first (same user restoring their own data).
            existing = Badge.objects.filter(uuid=uuid_str, profile=profile).first()
            if existing:
                badge_uuid_map[uuid_str] = existing.pk
                result.inc_skipped("badges")
                continue

            badge = Badge.objects.create(
                profile=profile,
                name=name,
                description=row.get("description") or "",
                color=row.get("color") or None,
                icon=row.get("icon") or None,
                kind=row.get("kind", "tag"),
                order=row.get("order", 0),
            )
            badge_uuid_map[uuid_str] = badge.pk
            result.inc_created("badges")
        else:
            # Global badge: match by name first, then fall back to creating as user badge.
            existing = Badge.objects.filter(profile__isnull=True, name=name).first()
            if existing:
                badge_uuid_map[uuid_str] = existing.pk
                result.inc_skipped("badges")
            else:
                # Re-create as a user-owned badge (global doesn't exist on this instance).
                user_existing = Badge.objects.filter(profile=profile, name=name).first()
                if user_existing:
                    badge_uuid_map[uuid_str] = user_existing.pk
                    result.inc_skipped("badges")
                else:
                    badge = Badge.objects.create(
                        profile=profile,
                        name=name,
                        description=row.get("description") or "",
                        color=row.get("color") or None,
                        icon=row.get("icon") or None,
                        kind=row.get("kind", "tag"),
                        order=row.get("order", 0),
                    )
                    badge_uuid_map[uuid_str] = badge.pk
                    result.inc_created("badges")

    # Second pass: wire up parent relationships now that all badges exist.
    for row in rows:
        uuid_str = row.get("uuid", "")
        if uuid_str not in badge_uuid_map:
            continue
        parent_uuids = row.get("parent_uuids", [])
        if not parent_uuids:
            continue
        try:
            badge = Badge.objects.get(pk=badge_uuid_map[uuid_str])
        except Badge.DoesNotExist:
            continue
        parent_pks = [badge_uuid_map[u] for u in parent_uuids if u in badge_uuid_map]
        if parent_pks:
            badge.parents.add(*parent_pks)


def _import_pins(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    badge_uuid_map: dict[str, int],
) -> None:
    """Import user pins and their linked locations."""
    from django.contrib.gis.geos import Point
    from django.db import IntegrityError

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

    rows = _read_json(data_dir, "pins.json")
    if not rows:
        return

    for row in rows:
        uuid_str = row.get("uuid", "")

        # Idempotency: skip pins that already exist for this user.
        existing = Pin.objects.filter(uuid=uuid_str).first()
        if existing:
            pin_uuid_map[uuid_str] = existing.pk
            result.inc_skipped("pins")
            continue

        # Resolve or create the linked Location.
        location = None
        loc_data = row.get("location")
        if loc_data:
            loc_uuid = loc_data.get("uuid", "")
            location = Location.objects.filter(uuid=loc_uuid).first() if loc_uuid else None
            if location is None:
                location = _create_location_from_data(loc_data)

        try:
            lat = row.get("latitude")
            lng = row.get("longitude")
            pin = Pin(
                profile=profile,
                location=location,
                name=row.get("name") or None,
                description=row.get("description") or "",
                icon=row.get("icon") or None,
                color=row.get("color") or None,
                priority=int(row.get("priority", 0)),
                is_private=bool(row.get("is_private", False)),
                pin_type=row.get("pin_type", "location"),
                latitude=lat or None,
                longitude=lng or None,
                detail_bg_color=row.get("detail_bg_color") or None,
                detail_bg_opacity=int(row.get("detail_bg_opacity", 80)),
                detail_border_color=row.get("detail_border_color") or None,
                detail_border_opacity=int(row.get("detail_border_opacity", 100)),
            )
            # Resolve effective coordinates for the GIS point field.
            eff_lat = float(lat) if lat else (float(location.latitude) if location else 0.0)
            eff_lng = float(lng) if lng else (float(location.longitude) if location else 0.0)
            pin.point = Point(eff_lng, eff_lat, srid=4326)

            pin.save()
            pin_uuid_map[uuid_str] = pin.pk
            result.inc_created("pins")

            # Assign badges.
            for badge_uuid in row.get("badge_uuids", []):
                if badge_uuid in badge_uuid_map:
                    pin.badges.add(badge_uuid_map[badge_uuid])

        except (IntegrityError, ValueError, TypeError):
            logger.warning("Failed to import pin %s", uuid_str, exc_info=True)
            result.warnings.append(f"Could not import pin '{row.get('name', uuid_str)}'.")


def _create_location_from_data(loc_data: dict[str, Any]) -> Any:
    """Create a new Location from exported location data."""
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.location.model import Location

    try:
        lat = float(loc_data.get("latitude") or 0)
        lng = float(loc_data.get("longitude") or 0)
        location = Location(
            name=loc_data.get("name", "Unknown Location"),
            description=loc_data.get("description") or "",
            latitude=lat,
            longitude=lng,
            street_number=loc_data.get("street_number") or None,
            route=loc_data.get("route") or None,
            locality=loc_data.get("locality") or None,
            administrative_area_level_1=loc_data.get("administrative_area_level_1") or None,
            administrative_area_level_2=loc_data.get("administrative_area_level_2") or None,
            administrative_area_level_3=loc_data.get("administrative_area_level_3") or None,
            country=loc_data.get("country") or "United States",
            zipcode=loc_data.get("zipcode") or None,
            zipcode_suffix=loc_data.get("zipcode_suffix") or None,
            point=Point(lng, lat, srid=4326),
        )
        location.save()
        return location
    except (ValueError, TypeError):
        logger.warning("Failed to create location from data: %s", loc_data.get("name"))
        return None


def _import_visit_history(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    badge_uuid_map: dict[str, int],
) -> None:
    """Import visit history records, skipping duplicates by (pin, visited_at)."""
    from django.utils.dateparse import parse_datetime

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit

    rows = _read_json(data_dir, "visit_history.json")
    if not rows:
        return

    for row in rows:
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
    badge_uuid_map: dict[str, int],
) -> None:
    """Import friendship connections. Skips connections to users not on this instance."""
    from urbanlens.dashboard.models.friendship.model import Friendship
    from urbanlens.dashboard.models.profile.model import Profile

    rows = _read_json(data_dir, "connections.json")
    if not rows:
        return

    for row in rows:
        other_uuid = row.get("other_user_uuid", "")
        direction = row.get("direction", "outgoing")

        other_profile = Profile.objects.filter(uuid=other_uuid).first()
        if other_profile is None:
            result.warnings.append(
                f"Skipped connection with '{row.get('other_username', other_uuid)}': user not found on this instance.",
            )
            result.inc_skipped("connections")
            continue

        from_p = profile if direction == "outgoing" else other_profile
        to_p = other_profile if direction == "outgoing" else profile

        if Friendship.objects.filter(from_profile=from_p, to_profile=to_p).exists():
            result.inc_skipped("connections")
            continue

        try:
            Friendship.objects.create(
                from_profile=from_p,
                to_profile=to_p,
                status=row.get("status", "Requested"),
                relationship_type=row.get("relationship_type", "Friend"),
                permissions=row.get("permissions", "View Profile"),
            )
            result.inc_created("connections")
        except Exception:
            logger.warning("Failed to import connection %s → %s", from_p, to_p, exc_info=True)
            result.warnings.append(f"Could not import connection with '{row.get('other_username', other_uuid)}'.")


def _import_settings(
    profile: Any,
    data_dir: str,
    result: ImportResult,
    *,
    pin_uuid_map: dict[str, int],
    badge_uuid_map: dict[str, int],
) -> None:
    """Import user settings, overwriting the current profile settings."""
    from urbanlens.dashboard.models.profile.model import Profile

    data = _read_json(data_dir, "settings.json")
    if not data:
        return

    privacy = data.get("privacy", {})
    update_fields: dict[str, Any] = {}

    _safe_set(update_fields, "theme_mode", data, Profile, "theme_mode")
    _safe_set(update_fields, "guidance_level", data, Profile, "guidance_level")
    _safe_set(update_fields, "map_dark_mode", data, Profile, "map_dark_mode")
    _safe_set(update_fields, "default_map_view", data, Profile, "default_map_view")
    _safe_set(update_fields, "map_center_mode", data, Profile, "map_center_mode")

    if "cluster_radius" in data:
        update_fields["cluster_radius"] = data["cluster_radius"]
    if "use_pin_cache" in data:
        update_fields["use_pin_cache"] = bool(data["use_pin_cache"])
    if "map_default_zoom" in data:
        update_fields["map_default_zoom"] = int(data["map_default_zoom"])
    if "markup_fill_color" in data:
        update_fields["markup_fill_color"] = data["markup_fill_color"]
    if "markup_fill_opacity" in data:
        update_fields["markup_fill_opacity"] = int(data["markup_fill_opacity"])
    if "markup_border_color" in data:
        update_fields["markup_border_color"] = data["markup_border_color"]
    if "markup_border_opacity" in data:
        update_fields["markup_border_opacity"] = int(data["markup_border_opacity"])

    for field_name in (
        "profile_visibility",
        "comment_visibility",
        "friend_request_visibility",
        "photo_upload_visibility",
        "viewer_photo_filter",
        "trip_pin_location_visibility",
        "contact_visibility",
    ):
        if field_name in privacy:
            update_fields[field_name] = privacy[field_name]

    if update_fields:
        Profile.objects.filter(pk=profile.pk).update(**update_fields)
        result.inc_created("settings")
    else:
        result.inc_skipped("settings")


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


# ── Dispatch table ─────────────────────────────────────────────────────────────

_IMPORT_ORDER = ["badges", "pins", "visit_history", "connections", "settings"]

_IMPORTERS: dict[str, Any] = {
    "badges": _import_badges,
    "pins": _import_pins,
    "visit_history": _import_visit_history,
    "connections": _import_connections,
    "settings": _import_settings,
}

_STEP_MESSAGES = {
    "badges": "Importing badges…",
    "pins": "Importing pins and locations…",
    "visit_history": "Importing visit history…",
    "connections": "Importing connections…",
    "settings": "Applying settings…",
}
