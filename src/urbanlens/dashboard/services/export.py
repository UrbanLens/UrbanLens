"""Data export service — build and manage user data export archives."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timezone
import io
import json
import logging
import os
import pathlib
import shutil
from typing import Any
import zipfile

from django.core.cache import cache

logger = logging.getLogger(__name__)

EXPORT_TTL_SECONDS = 3600

VALID_EXPORT_TYPES = frozenset(
    {
        "profile",
        "pins",
        "badges",
        "connections",
        "visit_history",
        "comments",
        "photos",
        "trips",
        "settings",
        "google_takeout",
    },
)

_ORDERED_TYPES = [
    "profile",
    "settings",
    "pins",
    "google_takeout",
    "badges",
    "connections",
    "visit_history",
    "comments",
    "photos",
    "trips",
]


def export_dir(job_id: str) -> str:
    """Return the filesystem path for a given export job."""
    from django.conf import settings

    return os.path.join(settings.MEDIA_ROOT, "exports", job_id)


class ExportJobStatus:
    """Cache-backed progress state for a user export job.

    The export archive remains on disk as the final downloadable artifact; transient
    status lives in the application cache rather than a JSON sidecar in MEDIA_ROOT.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.cache_key = f"dashboard:export:{job_id}:status"

    def write(self, status: str, progress: int, message: str, user_id: int | None = None) -> None:
        """Write (or update) the job status in cache."""
        existing = self.read()
        data: dict[str, Any] = {"status": status, "progress": progress, "message": message}
        if user_id is not None:
            data["user_id"] = user_id
        elif "user_id" in existing:
            data["user_id"] = existing["user_id"]
        cache.set(self.cache_key, data, timeout=EXPORT_TTL_SECONDS)

    def read(self) -> dict[str, Any]:
        """Return the current job status dict, or an empty dict when not found."""
        return cache.get(self.cache_key) or {}

    def delete(self) -> None:
        """Remove the job status from cache."""
        cache.delete(self.cache_key)


def cleanup_export_artifacts(export_dir_path: str, job_status: ExportJobStatus | None = None) -> None:
    """Remove an export directory and optional cache-backed job status."""
    shutil.rmtree(export_dir_path, ignore_errors=True)
    if job_status is not None:
        job_status.delete()


def schedule_export_cleanup(export_dir_path: str, job_status: ExportJobStatus | None = None) -> None:
    """Schedule export cleanup through Celery; fall back to logging on enqueue failure."""
    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import cleanup_export_artifacts_task

    result = safely_enqueue_task(
        cleanup_export_artifacts_task,
        export_dir_path,
        job_status.job_id if job_status is not None else None,
        countdown=EXPORT_TTL_SECONDS,
    )
    if result is None:
        logger.warning("Unable to schedule cleanup for export directory %s", export_dir_path)


def run_export(user_id: int, export_types: list[str], export_dir_path: str, base_url: str, *, job_id: str | None = None) -> bool:
    """Run all export steps for a user and return True on success.

    Args:
        user_id: PK of the user whose data to export.
        export_types: Subset of ``VALID_EXPORT_TYPES``.
        export_dir_path: Filesystem path for this job (created by ``export_dir(job_id)``).
        base_url: Absolute site root URL, used to build pin detail URLs.
        job_id: UUID string for this export job. Derived from ``export_dir_path``
            basename when not provided, but callers should always pass it explicitly.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ObjectDoesNotExist

    User = get_user_model()
    resolved_job_id = job_id or pathlib.Path(export_dir_path).name

    try:
        user = User.objects.select_related("profile").get(pk=user_id)
        profile = user.profile
    except (ObjectDoesNotExist, AttributeError):
        logger.exception("Export: could not load user %s", user_id)
        ExportJobStatus(resolved_job_id).write("error", 0, "Failed to load user data.")
        schedule_export_cleanup(export_dir_path, ExportJobStatus(resolved_job_id))
        return False

    temp_dir = os.path.join(export_dir_path, "data")
    os.makedirs(temp_dir, exist_ok=True)

    total_steps = len(export_types) + 1  # +1 for zipping
    step = 0

    exporters: dict[str, tuple[Any, str]] = {
        "profile": (_export_profile, "Exporting profile…"),
        "settings": (_export_settings, "Exporting settings…"),
        "pins": (_export_pins, "Exporting pins…"),
        "google_takeout": (_export_pins_google_takeout, "Exporting Google Takeout format…"),
        "badges": (_export_badges, "Exporting badges…"),
        "connections": (_export_connections, "Exporting connections…"),
        "visit_history": (_export_visit_history, "Exporting visit history…"),
        "comments": (_export_comments, "Exporting comments…"),
        "photos": (_export_photos, "Exporting photos…"),
        "trips": (_export_trips, "Exporting trips…"),
    }

    try:
        _run_export_steps(
            profile,
            export_types,
            exporters,
            step,
            total_steps,
            job_id=resolved_job_id,
            export_dir_path=export_dir_path,
            temp_dir=temp_dir,
            base_url=base_url,
        )
        return True
    except Exception:
        logger.exception("Export failed for user %s", user_id)
        ExportJobStatus(resolved_job_id).write("error", 0, "Export failed. Please try again.")
        return False
    finally:
        schedule_export_cleanup(export_dir_path, ExportJobStatus(resolved_job_id))


def _run_export_steps(
    profile: Any,
    export_types: list[str],
    exporters: dict[str, Any],
    step: int,
    total_steps: int,
    *,
    job_id: str,
    export_dir_path: str,
    temp_dir: str,
    base_url: str,
) -> None:
    _write_manifest(profile, temp_dir, export_types)

    for key in _ORDERED_TYPES:
        if key not in export_types:
            continue
        fn, msg = exporters[key]
        ExportJobStatus(job_id).write("running", max(5, int(step / total_steps * 85)), msg)
        fn(profile, temp_dir, base_url=base_url)
        step += 1

    ExportJobStatus(job_id).write("running", 90, "Creating archive…")
    _build_zip(export_dir_path, temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)
    ExportJobStatus(job_id).write("done", 100, "Export ready!")


def _resolve_target(obj: Any) -> tuple[str, str]:
    """Return (target_type, target_name) for an object with a pin or location FK."""
    if obj.pin:
        name = obj.pin.name or (obj.pin.location.name if obj.pin.location else "")
        return "pin", name
    if obj.location:
        return "location", obj.location.name
    return "", ""


def _build_zip(export_dir_path: str, temp_dir: str) -> None:
    today = date.today().isoformat()
    zip_path = os.path.join(export_dir_path, "export.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(temp_dir):
            for filename in files:
                file_path = os.path.join(root, filename)
                arcname = os.path.join(f"urbanlens_export_{today}", os.path.relpath(file_path, temp_dir))
                zf.write(file_path, arcname)


# ── Manifest ──────────────────────────────────────────────────────────────────


def _write_manifest(profile: Any, temp_dir: str, export_types: list[str]) -> None:
    data = {
        "format": "urbanlens_v1",
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "user_uuid": str(profile.uuid),
        "username": profile.username,
        "contents": export_types,
    }
    with open(os.path.join(temp_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


# ── Individual exporters ───────────────────────────────────────────────────────


def _export_profile(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    data = {
        "username": profile.user.username,
        "email": profile.user.email,
        "first_name": profile.user.first_name,
        "last_name": profile.user.last_name,
        "bio": profile.bio or "",
        "area": profile.area or "",
        "birth_date": str(profile.birth_date) if profile.birth_date else None,
        "started_exploring": str(profile.started_exploring) if profile.started_exploring else None,
        "date_joined": str(profile.user.date_joined),
    }
    with open(os.path.join(temp_dir, "profile.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _export_settings(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    data = {
        "theme_mode": profile.theme_mode,
        "guidance_level": profile.guidance_level,
        "map_dark_mode": profile.map_dark_mode,
        "default_map_view": profile.default_map_view,
        "cluster_radius": profile.cluster_radius,
        "use_pin_cache": profile.use_pin_cache,
        "map_center_mode": profile.map_center_mode,
        "map_default_zoom": profile.map_default_zoom,
        "markup_fill_color": profile.markup_fill_color,
        "markup_fill_opacity": profile.markup_fill_opacity,
        "markup_border_color": profile.markup_border_color,
        "markup_border_opacity": profile.markup_border_opacity,
        "privacy": {
            "profile_visibility": profile.profile_visibility,
            "comment_visibility": profile.comment_visibility,
            "friend_request_visibility": profile.friend_request_visibility,
            "photo_upload_visibility": profile.photo_upload_visibility,
            "viewer_photo_filter": profile.viewer_photo_filter,
            "trip_pin_location_visibility": profile.trip_pin_location_visibility,
            "contact_visibility": profile.contact_visibility,
        },
    }
    with open(os.path.join(temp_dir, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _export_pins(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export all user pins as a rich JSON file (UrbanLens custom format)."""
    from urbanlens.dashboard.models.pin.model import Pin

    pins = (
        Pin.objects.filter(profile=profile)
        .select_related("location")
        .prefetch_related("badges")
        .order_by("created")
    )

    rows = []
    for pin in pins:
        location_data = None
        if pin.location:
            loc = pin.location
            location_data = {
                "uuid": str(loc.uuid),
                "name": loc.name,
                "description": loc.description or "",
                "latitude": str(loc.latitude),
                "longitude": str(loc.longitude),
                "street_number": loc.street_number or "",
                "route": loc.route or "",
                "locality": loc.locality or "",
                "administrative_area_level_1": loc.administrative_area_level_1 or "",
                "administrative_area_level_2": loc.administrative_area_level_2 or "",
                "administrative_area_level_3": loc.administrative_area_level_3 or "",
                "country": loc.country or "",
                "zipcode": loc.zipcode or "",
                "zipcode_suffix": loc.zipcode_suffix or "",
                "date_abandoned": str(loc.date_abandoned) if loc.date_abandoned else None,
                "date_last_active": str(loc.date_last_active) if loc.date_last_active else None,
            }

        rows.append(
            {
                "uuid": str(pin.uuid),
                "name": pin.name,
                "description": pin.description or "",
                "icon": pin.icon or "",
                "color": pin.color or "",
                "priority": pin.priority,
                "is_private": pin.is_private,
                "pin_type": pin.pin_type,
                "latitude": str(pin.latitude) if pin.latitude else None,
                "longitude": str(pin.longitude) if pin.longitude else None,
                "last_visited": str(pin.last_visited) if pin.last_visited else None,
                "date_abandoned": str(pin.date_abandoned) if pin.date_abandoned else None,
                "date_last_active": str(pin.date_last_active) if pin.date_last_active else None,
                "detail_bg_color": pin.detail_bg_color or "",
                "detail_bg_opacity": pin.detail_bg_opacity,
                "detail_border_color": pin.detail_border_color or "",
                "detail_border_opacity": pin.detail_border_opacity,
                "created": str(pin.created),
                "updated": str(pin.updated),
                "badge_uuids": [str(b.uuid) for b in pin.badges.all()],
                "location": location_data,
            },
        )

    with open(os.path.join(temp_dir, "pins.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_pins_google_takeout(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export pins as a Google Takeout-compatible CSV file."""
    from urbanlens.dashboard.models.pin.model import Pin

    pins = (
        Pin.objects.filter(profile=profile)
        .select_related("location")
        .prefetch_related("badges")
        .order_by("created")
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Title", "Note", "URL", "Tags", "Comment"])

    for pin in pins:
        name = pin.name or (pin.location.name if pin.location else "")
        note = pin.description or ""
        url = f"{base_url.rstrip('/')}/dashboard/map/pin/{pin.slug}/" if pin.slug else ""
        tags = ", ".join(b.name for b in pin.badges.all() if hasattr(b, "name"))
        writer.writerow([name, note, url, tags, ""])

    gt_dir = os.path.join(temp_dir, "google_takeout")
    os.makedirs(gt_dir, exist_ok=True)
    pathlib.Path(os.path.join(gt_dir, "pins.csv")).write_text(buf.getvalue(), encoding="utf-8", newline="")


def _export_badges(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export all badges visible to the user, with pin assignments."""
    from urbanlens.dashboard.models.badges.model import Badge

    # Export user-owned badges plus global badges that are assigned to the user's pins.
    user_badges = Badge.objects.filter(profile=profile).prefetch_related("parents", "pins")
    global_assigned = (
        Badge.objects.filter(profile__isnull=True, pins__profile=profile)
        .distinct()
        .prefetch_related("parents", "pins")
    )

    seen: set[int] = set()
    rows = []

    for badge in list(user_badges) + list(global_assigned):
        if badge.pk in seen:
            continue
        seen.add(badge.pk)

        rows.append(
            {
                "uuid": str(badge.uuid),
                "name": badge.name,
                "description": badge.description or "",
                "color": badge.color or "",
                "icon": badge.icon or "",
                "kind": badge.kind,
                "order": badge.order,
                "is_user_badge": badge.profile_id is not None,
                "is_protected": badge.is_protected,
                "parent_uuids": [str(p.uuid) for p in badge.parents.all()],
                "pin_uuids": [
                    str(p.uuid)
                    for p in badge.pins.filter(profile=profile)
                ],
            },
        )

    with open(os.path.join(temp_dir, "badges.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_connections(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export friendship connections as a list of relationship records."""
    from urbanlens.dashboard.models.friendship.model import Friendship

    friendships = (
        Friendship.objects.filter(from_profile=profile)
        .select_related("to_profile__user")
        .order_by("created")
    )
    incoming = (
        Friendship.objects.filter(to_profile=profile)
        .select_related("from_profile__user")
        .order_by("created")
    )

    rows = []
    for f in friendships:
        rows.append(
            {
                "other_user_uuid": str(f.to_profile.uuid),
                "other_username": f.to_profile.username,
                "status": f.status,
                "relationship_type": f.relationship_type,
                "permissions": f.permissions,
                "direction": "outgoing",
                "created": str(f.created),
            },
        )
    for f in incoming:
        rows.append(
            {
                "other_user_uuid": str(f.from_profile.uuid),
                "other_username": f.from_profile.username,
                "status": f.status,
                "relationship_type": f.relationship_type,
                "permissions": f.permissions,
                "direction": "incoming",
                "created": str(f.created),
            },
        )

    with open(os.path.join(temp_dir, "connections.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_visit_history(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export all visit history records for the user's pins."""
    from urbanlens.dashboard.models.visits.model import PinVisit

    visits = (
        PinVisit.objects.filter(pin__profile=profile)
        .select_related("pin")
        .order_by("visited_at")
    )

    rows = [
        {
            "uuid": str(v.uuid),
            "pin_uuid": str(v.pin.uuid),
            "visited_at": str(v.visited_at),
            "notes": v.notes or "",
            "source": v.source,
        }
        for v in visits
    ]

    with open(os.path.join(temp_dir, "visit_history.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_comments(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    from urbanlens.dashboard.models.comments.model import Comment

    comments = (
        Comment.objects.filter(profile=profile)
        .select_related("pin__location", "location")
        .order_by("created")
    )

    rows = []
    for comment in comments:
        target_type, target = _resolve_target(comment)
        rows.append(
            {
                "uuid": str(comment.uuid),
                "target_type": target_type,
                "target_name": target,
                "text": comment.text,
                "created": str(comment.created),
            },
        )

    with open(os.path.join(temp_dir, "comments.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_photos(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    from urbanlens.dashboard.models.images.model import Image

    images = Image.objects.filter(profile=profile).select_related("pin__location", "location").order_by("created")

    photos_dir = os.path.join(temp_dir, "photos")
    os.makedirs(photos_dir, exist_ok=True)

    metadata = []
    for image in images:
        target_type, target = _resolve_target(image)
        file_path = image.image.path if image.image else None
        filename = os.path.basename(file_path) if file_path else None

        if file_path and filename is not None and os.path.exists(file_path):
            dest = os.path.join(photos_dir, filename)
            if os.path.exists(dest):
                base, ext = os.path.splitext(filename)
                dest = os.path.join(photos_dir, f"{base}_{image.pk}{ext}")
                filename = os.path.basename(dest)
            shutil.copy2(file_path, dest)

        metadata.append(
            {
                "uuid": str(image.uuid),
                "filename": filename,
                "caption": image.caption or "",
                "target_type": target_type,
                "target_name": target,
                "latitude": str(image.latitude) if image.latitude else None,
                "longitude": str(image.longitude) if image.longitude else None,
                "created": str(image.created),
            },
        )

    with open(os.path.join(photos_dir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False)


def _export_trips(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    from urbanlens.dashboard.models.trips.model import Trip

    trips = (
        Trip.objects.filter(profiles=profile)
        .prefetch_related("profiles__user")
        .select_related("creator__user")
        .order_by("created")
    )

    rows = []
    for trip in trips:
        rows.append(
            {
                "uuid": str(trip.uuid),
                "name": trip.name,
                "description": trip.description or "",
                "start_date": str(trip.start_date) if trip.start_date else None,
                "end_date": str(trip.end_date) if trip.end_date else None,
                "creator": trip.creator.user.username if trip.creator else None,
                "members": [p.user.username for p in trip.profiles.all()],
                "created": str(trip.created),
            },
        )

    with open(os.path.join(temp_dir, "trips.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
