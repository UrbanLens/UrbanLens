"""Data export service - build and manage user data export archives."""

from __future__ import annotations

from abc import ABC, abstractmethod
import csv
from datetime import UTC, date, datetime
import io
import json
import logging
import os
import pathlib
import shutil
from typing import TYPE_CHECKING, Any, ClassVar
import zipfile

from django.core.cache import cache
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import Model

    from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
    from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
    from urbanlens.dashboard.models.routes.model import Route
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter

logger = logging.getLogger(__name__)

EXPORT_TTL_SECONDS = 3600

#: Largest export ZIP that gets attached to the "your export is ready" email
#: directly (UL-373); anything bigger gets a download link instead, so the
#: email never blows past typical mailbox attachment limits.
EMAIL_ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024

#: The export areas that predate the declarative registry at the bottom of this
#: module - each is a hand-written ``_export_*`` function wired into
#: :func:`run_export` by name. Listed in the order they run. New areas are added
#: as an :class:`ExportType` subclass instead; ``VALID_EXPORT_TYPES`` and
#: ``_ORDERED_TYPES`` are derived from both halves (see ``_REGISTERED_EXPORT_TYPES``).
_LEGACY_ORDERED_TYPES: tuple[str, ...] = (
    "profile",
    "settings",
    "custom_fields",
    "pins",
    "google_takeout",
    "labels",
    "connections",
    "visit_history",
    "comments",
    "photos",
    "trips",
    "pin_lists",
    "direct_messages",
)


def export_dir(job_id: str) -> str:
    """Return the filesystem path for a given export job."""
    from django.conf import settings as django_settings

    return os.path.join(django_settings.MEDIA_ROOT, "exports", job_id)


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
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import cleanup_export_artifacts_task

    result = safely_enqueue_task(
        cleanup_export_artifacts_task,
        export_dir_path,
        job_status.job_id if job_status is not None else None,
        countdown=EXPORT_TTL_SECONDS,
    )
    if result is None:
        logger.warning("Unable to schedule cleanup for export directory %s", export_dir_path)


def run_export(user_id: int, export_types: list[str], export_dir_path: str, base_url: str, *, job_id: str | None = None, email_to_user: bool = False) -> bool:
    """Run all export steps for a user and return True on success.

    Args:
        user_id: PK of the user whose data to export.
        export_types: Subset of ``VALID_EXPORT_TYPES``.
        export_dir_path: Filesystem path for this job (created by ``export_dir(job_id)``).
        base_url: Absolute site root URL, used to build pin detail URLs.
        job_id: UUID string for this export job. Derived from ``export_dir_path``
            basename when not provided, but callers should always pass it explicitly.
        email_to_user: When True, email the finished export to the user's account
            address (UL-373) - see :func:`send_export_email`. Email problems (no
            address on file, delivery failure) never fail the export itself; the
            outcome is surfaced through the job status message instead.
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
        "profile": (_export_profile, "Exporting profile..."),
        "settings": (_export_settings, "Exporting settings..."),
        "custom_fields": (_export_custom_fields, "Exporting custom fields..."),
        "pins": (_export_pins, "Exporting pins..."),
        "google_takeout": (_export_pins_google_takeout, "Exporting Google Takeout format..."),
        "labels": (_export_labels, "Exporting labels..."),
        "connections": (_export_connections, "Exporting connections..."),
        "visit_history": (_export_visit_history, "Exporting visit history..."),
        "comments": (_export_comments, "Exporting comments..."),
        "photos": (_export_photos, "Exporting photos..."),
        "trips": (_export_trips, "Exporting trips..."),
        "pin_lists": (_export_pin_lists, "Exporting lists..."),
        "direct_messages": (_export_direct_messages, "Exporting direct messages..."),
        # Registry-backed areas declare their own progress message; see ExportType.
        **{key: (export_type, export_type.message) for key, export_type in _REGISTERED_EXPORTERS.items()},
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
            email_to_user=email_to_user,
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
    email_to_user: bool = False,
) -> None:
    _write_manifest(profile, temp_dir, export_types)

    for key in _ORDERED_TYPES:
        if key not in export_types:
            continue
        fn, msg = exporters[key]
        ExportJobStatus(job_id).write("running", max(5, int(step / total_steps * 85)), msg)
        fn(profile, temp_dir, base_url=base_url)
        step += 1

    ExportJobStatus(job_id).write("running", 90, "Creating archive...")
    _build_zip(export_dir_path, temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)

    message = "Export ready!"
    if email_to_user:
        ExportJobStatus(job_id).write("running", 95, "Sending email...")
        email_note = send_export_email(profile.user, export_dir_path, base_url, job_id=job_id)
        message = f"Export ready! {email_note}"
    ExportJobStatus(job_id).write("done", 100, message)


def _resolve_target(obj: Any) -> tuple[str, str, str]:
    """Return (target_type, target_name, target_uuid) for an object with a pin or wiki FK.

    ``target_uuid`` is what the importer matches on (names are neither unique
    nor stable); the name is kept for human readability of the archive.
    """
    if obj.pin:
        return "pin", obj.pin.effective_name, str(obj.pin.uuid)
    wiki = getattr(obj, "wiki", None)
    if wiki:
        return "location", wiki.name, str(wiki.uuid)
    return "", "", ""


def _build_zip(export_dir_path: str, temp_dir: str) -> None:
    today = timezone.localdate().isoformat()
    zip_path = os.path.join(export_dir_path, "export.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(temp_dir):
            for filename in files:
                file_path = os.path.join(root, filename)
                arcname = os.path.join(f"urbanlens_export_{today}", os.path.relpath(file_path, temp_dir))
                zf.write(file_path, arcname)


def send_export_email(user: Any, export_dir_path: str, base_url: str, *, job_id: str) -> str:
    """Email the finished export ZIP to the user's account address (UL-373).

    Small archives (up to ``EMAIL_ATTACHMENT_MAX_BYTES``) are attached
    directly; larger ones get a link to the existing authenticated download
    endpoint instead, which stays valid until the job's artifacts expire
    (``EXPORT_TTL_SECONDS``). Follows the established outbound-email pattern
    (see ``services.profile.account_deletion._send_email``): ``EmailMultiAlternatives``
    with the site's default from-address, HTML alternative rendered from a
    ``dashboard/email/`` template, and delivery failures logged rather than
    raised - an email problem must never fail an otherwise-finished export.

    Args:
        user: The Django user the export belongs to.
        export_dir_path: Filesystem path of the job directory holding ``export.zip``.
        base_url: Absolute site root URL (same value the export pipeline receives).
        job_id: UUID string for this export job, used to build the download URL.

    Returns:
        A short user-facing sentence describing the outcome, appended to the
        job's "done" status message (emailed as attachment, emailed as link,
        no address on file, or send failure).
    """
    import smtplib

    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.urls import reverse

    email = getattr(user, "email", "") or ""
    if not email:
        logger.info("Export email requested but user %s has no email address; skipping", getattr(user, "pk", None))
        return "Your account has no email address, so the export was not emailed."

    zip_path = os.path.join(export_dir_path, "export.zip")
    try:
        zip_size = os.path.getsize(zip_path)
    except OSError:
        logger.exception("Export email: archive missing for job %s", job_id)
        return "The export email could not be sent."

    attach = zip_size <= EMAIL_ATTACHMENT_MAX_BYTES
    download_url = f"{base_url.rstrip('/')}{reverse('tools.export.download', kwargs={'job_id': job_id})}"
    today = timezone.localdate().isoformat()
    filename = f"urbanlens_export_{today}.zip"

    subject = "Your UrbanLens data export is ready"
    if attach:
        text_body = f"Your UrbanLens data export is attached.\n\nIt is also available to download for about an hour: {download_url}"
    else:
        text_body = f"Your UrbanLens data export is ready.\n\nThe archive was too large to attach, so download it here (available for about an hour): {download_url}"
    html_body = render_to_string(
        "dashboard/email/export_ready.html",
        {"attached": attach, "download_url": download_url, "filename": filename},
    )

    try:
        msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[email])
        msg.attach_alternative(html_body, "text/html")
        if attach:
            with open(zip_path, "rb") as fh:
                msg.attach(filename, fh.read(), "application/zip")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send export email for job %s to user %s", job_id, getattr(user, "pk", None))
        return "The export email could not be sent - you can still download it below."

    if attach:
        return "A copy was emailed to you."
    return "A download link was emailed to you (the archive was too large to attach)."


def _write_json(temp_dir: str, filename: str, data: Any) -> None:
    """Write one JSON file into the archive with the settings every exporter uses.

    Args:
        temp_dir: The archive's staging directory.
        filename: Path relative to ``temp_dir`` (its parent must already exist).
        data: Any JSON-serializable payload.
    """
    with open(os.path.join(temp_dir, filename), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _copy_into_archive(source_path: str | None, dest_dir: str, unique_suffix: Any) -> str | None:
    """Copy a stored media file into the archive, disambiguating name collisions.

    Mirrors the file handling in :func:`_export_photos`: two rows can hold files
    with the same basename, so a collision gets the row's own identifier
    appended rather than silently overwriting the first copy.

    Args:
        source_path: Absolute path of the stored file, or None/"" when absent.
        dest_dir: Directory inside the archive to copy into (created by caller).
        unique_suffix: Value appended to the stem on a name collision.

    Returns:
        The archive-relative filename that was written, or None when there was
        no readable source file.
    """
    if not source_path or not os.path.exists(source_path):
        return None
    filename = os.path.basename(source_path)
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest):
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{unique_suffix}{ext}"
        dest = os.path.join(dest_dir, filename)
    shutil.copy2(source_path, dest)
    return filename


# -- Manifest ------------------------------------------------------------------


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


# -- Individual exporters -------------------------------------------------------


def _export_profile(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export the profile's identity block, free-text content, and contact details.

    Also carries the two other profile-scoped things a user authors by hand:
    ``social_links`` (one link per platform) and ``secondary_emails`` (extra
    addresses they can be found by). Verification state travels with the
    secondary addresses for readability but is deliberately not re-importable -
    see ``ProfileImport``. Neither the verification token nor any other
    credential is exported.
    """
    from urbanlens.dashboard.models.profile.email import ProfileEmail
    from urbanlens.dashboard.models.social_link.model import SocialLink

    social_links = SocialLink.objects.filter(profile=profile).order_by("platform")
    secondary_emails = ProfileEmail.objects.filter(profile=profile).order_by("created")

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
        "contact": {
            "phone_number": profile.phone_number or "",
            "signal_username": profile.signal_username or "",
            "discord_username": profile.discord_username or "",
            "whatsapp_number": profile.whatsapp_number or "",
            "telegram_username": profile.telegram_username or "",
            "matrix_handle": profile.matrix_handle or "",
        },
        "social_links": [{"platform": link.platform, "handle": link.handle} for link in social_links],
        "secondary_emails": [
            {
                "email": row.email or "",
                "is_verified": row.is_verified,
                "verified_at": str(row.verified_at) if row.verified_at else None,
                "created": str(row.created),
            }
            for row in secondary_emails
        ],
    }
    _write_json(temp_dir, "profile.json", data)


def _export_settings(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export every user-configurable Profile setting, plus per-notification-type delivery preferences.

    Deliberately excludes fields that aren't really "settings": identity/PII
    (covered by profile.json), internal bookkeeping (deletion_requested_at,
    profile_setup_complete, tos_accepted_at, ...), and anything security-
    sensitive (there is none stored directly on Profile - passkeys/TOTP/etc.
    live in their own models and are never exported).
    """
    data = {
        "theme_mode": profile.theme_mode,
        "guidance_level": profile.guidance_level,
        "distance_units": profile.distance_units,
        "map_dark_mode": profile.map_dark_mode,
        "default_map_view": profile.default_map_view,
        "cluster_radius": profile.cluster_radius,
        "use_pin_cache": profile.use_pin_cache,
        "map_center_mode": profile.map_center_mode,
        "map_default_zoom": profile.map_default_zoom,
        "map_center_latitude": str(profile.map_center_latitude) if profile.map_center_latitude is not None else None,
        "map_center_longitude": str(profile.map_center_longitude) if profile.map_center_longitude is not None else None,
        "map_custom_latitude": str(profile.map_custom_latitude) if profile.map_custom_latitude is not None else None,
        "map_custom_longitude": str(profile.map_custom_longitude) if profile.map_custom_longitude is not None else None,
        "remembered_map_lat": str(profile.remembered_map_lat) if profile.remembered_map_lat is not None else None,
        "remembered_map_lng": str(profile.remembered_map_lng) if profile.remembered_map_lng is not None else None,
        "remembered_map_zoom": profile.remembered_map_zoom,
        "markup_fill_color": profile.markup_fill_color,
        "markup_fill_opacity": profile.markup_fill_opacity,
        "markup_border_color": profile.markup_border_color,
        "markup_border_opacity": profile.markup_border_opacity,
        "pin_detail_map_height": profile.pin_detail_map_height,
        "media_gallery_sort": profile.media_gallery_sort,
        "show_wiki_cover_photos": profile.show_wiki_cover_photos,
        "auto_create_pin_article_from_wikipedia": profile.auto_create_pin_article_from_wikipedia,
        "ai": {
            "ai_enabled": profile.ai_enabled,
            "ai_label_tags": profile.ai_label_tags,
            "ai_label_categories": profile.ai_label_categories,
            "ai_label_statuses": profile.ai_label_statuses,
        },
        "keyword_tagging": {
            "keyword_tagging_enabled": profile.keyword_tagging_enabled,
            "keyword_label_tags": profile.keyword_label_tags,
            "keyword_label_categories": profile.keyword_label_categories,
            "keyword_label_statuses": profile.keyword_label_statuses,
        },
        "photos": {
            "generate_photo_keywords": profile.generate_photo_keywords,
            "image_downscale_max_dimension": profile.image_downscale_max_dimension,
            "video_downscale_max_height": profile.video_downscale_max_height,
        },
        "places_layers": {
            "places_google_enabled": profile.places_google_enabled,
            "places_nps_enabled": profile.places_nps_enabled,
            "places_wikipedia_enabled": profile.places_wikipedia_enabled,
        },
        "tracking": {
            "track_pin_visits": profile.track_pin_visits,
            "track_routes": profile.track_routes,
            "track_geolocation": profile.track_geolocation,
        },
        "community": {
            "community_enabled": profile.community_enabled,
            "sync_rating_to_wiki": profile.sync_rating_to_wiki,
            "sync_vulnerability_to_wiki": profile.sync_vulnerability_to_wiki,
            "sync_priority_to_wiki": profile.sync_priority_to_wiki,
            "sync_danger_to_wiki": profile.sync_danger_to_wiki,
            "sync_aliases": profile.sync_aliases,
        },
        "external_apis_enabled": profile.external_apis_enabled,
        "privacy": {
            "profile_visibility": profile.profile_visibility,
            "comment_visibility": profile.comment_visibility,
            "friend_request_visibility": profile.friend_request_visibility,
            "photo_upload_visibility": profile.photo_upload_visibility,
            "viewer_photo_filter": profile.viewer_photo_filter,
            "trip_pin_location_visibility": profile.trip_pin_location_visibility,
            "contact_visibility": profile.contact_visibility,
            "direct_message_visibility": profile.direct_message_visibility,
            "online_status_visibility": profile.online_status_visibility,
            "read_receipt_visibility": profile.read_receipt_visibility,
            "typing_indicator_visibility": profile.typing_indicator_visibility,
            "common_pins_visibility": profile.common_pins_visibility,
            "direct_message_delete_after": profile.direct_message_delete_after,
            "allow_friend_recommendations": profile.allow_friend_recommendations,
        },
        "notification_preferences": _notification_preferences_dict(profile),
    }
    with open(os.path.join(temp_dir, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _notification_preferences_dict(profile: Any) -> dict[str, Any]:
    """Every field on the user's NotificationPreference row (delivery channel per notification type).

    Introspected via the model's own field list rather than hand-enumerated:
    every field here really is a plain delivery-channel setting (no PII, no
    relations besides the owning profile), so this stays correct automatically
    as new notification types are added - unlike the rest of this file, which
    hand-lists fields deliberately so a new *sensitive* Profile field is never
    exported without a human noticing.
    """
    from urbanlens.dashboard.models.notifications.model import NotificationPreference

    prefs = NotificationPreference.objects.filter(profile=profile).first()
    if prefs is None:
        return {}
    skip = {"id", "profile", "created", "updated", "uuid"}
    return {f.name: getattr(prefs, f.name) for f in NotificationPreference._meta.get_fields() if getattr(f, "concrete", False) and f.name not in skip}  # noqa: SLF001


def _export_custom_fields(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export the user's custom field definitions and every stored value.

    Each field row carries its values inline, with targets referenced by UUID
    (matching the UUIDs used in the other export files) plus a human-readable
    label so the export is useful on its own.
    """
    from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity

    fields = (
        CustomField.objects.filter(profile=profile)
        .order_by("entity_type", "order", "name")
        .prefetch_related(
            "values__pin",
            "values__image",
            "values__target_profile",
            "values__markup_map",
        )
    )

    rows = []
    for field in fields:
        values = []
        for value in field.values.all():
            if field.entity_type == CustomFieldEntity.PIN and value.pin:
                target_uuid, target_label = str(value.pin.uuid), value.pin.effective_name
            elif field.entity_type == CustomFieldEntity.PHOTO and value.image:
                target_uuid, target_label = str(value.image.uuid), value.image.caption or ""
            elif field.entity_type == CustomFieldEntity.PROFILE and value.target_profile:
                target_uuid, target_label = str(value.target_profile.uuid), value.target_profile.username
            elif field.entity_type == CustomFieldEntity.MARKUP_MAP and value.markup_map:
                target_uuid, target_label = str(value.markup_map.uuid), value.markup_map.title or ""
            else:
                continue
            values.append(
                {
                    "target_type": field.entity_type,
                    "target_uuid": target_uuid,
                    "target_label": target_label,
                    "value": value.export_value(),
                },
            )
        rows.append(
            {
                "uuid": str(field.uuid),
                "entity_type": field.entity_type,
                "name": field.name,
                "field_type": field.field_type,
                "style": field.style,
                "config": field.config or {},
                "created": str(field.created),
                "values": values,
            },
        )

    with open(os.path.join(temp_dir, "custom_fields.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_pins(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export all user pins as a rich JSON file (UrbanLens custom format).

    Only personal Pin data is exported here - never the shared Location or Wiki it may
    be linked to. Community wiki data (canonical name, address, description) belongs
    to the instance, not to any one user's export.
    Each pin's *effective* coordinates are exported instead of its raw
    lat/lng override, so a pin that currently relies on its Location for
    placement still has somewhere to land on import. On import, pins are
    re-linked to an existing nearby Location or get a new one created, the
    same way a manually-added pin or a Google Takeout import would be.

    Also carries the pin's own review rating, security indicators (fences,
    alarms, cameras, ...), and private article (if any) - a pin article is
    only ever visible to its owner (see ``models.article.Article.is_private``),
    so it's fully covered by exporting it alongside the rest of this pin.

    ``PinAlias`` rows ride along in each pin's ``aliases`` list rather than in a
    file of their own: an alias is scoped to one pin (it has no profile FK -
    ownership is derived from ``pin.profile``) and means nothing detached from
    it. Every alias is exported, including the ``official`` ones synced from
    external name providers, so the archive shows every name the pin has ever
    carried; only user-authored ones are restored on import.
    """
    from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.reviews.model import Review

    pins = Pin.objects.filter(profile=profile).select_related("location", "article").prefetch_related("labels", "aliases").order_by("created")
    ratings = dict(Review.objects.filter(profile=profile).values_list("pin_id", "rating"))

    rows = []
    for pin in pins:
        article = getattr(pin, "article", None)
        rows.append(
            {
                "uuid": str(pin.uuid),
                "name": pin.name,
                "description": pin.description or "",
                "icon": pin.icon or "",
                "color": pin.color or "",
                "priority": pin.priority,
                "vulnerability": pin.vulnerability,
                "danger": pin.danger,
                "rating": ratings.get(pin.pk),
                "security": {field_name: getattr(pin, field_name) for field_name, _label in SECURITY_FIELDS},
                "pin_type": pin.pin_type,
                "latitude": str(pin.effective_latitude) if pin.effective_latitude is not None else None,
                "longitude": str(pin.effective_longitude) if pin.effective_longitude is not None else None,
                "last_visited": str(pin.last_visited) if pin.last_visited else None,
                "date_built": str(pin.date_built) if pin.date_built else None,
                "date_abandoned": str(pin.date_abandoned) if pin.date_abandoned else None,
                "date_last_active": str(pin.date_last_active) if pin.date_last_active else None,
                "detail_bg_color": pin.detail_bg_color or "",
                "detail_bg_opacity": pin.detail_bg_opacity,
                "detail_border_color": pin.detail_border_color or "",
                "detail_border_opacity": pin.detail_border_opacity,
                "created": str(pin.created),
                "updated": str(pin.updated),
                "label_uuids": [str(b.uuid) for b in pin.labels.all()],
                "aliases": [{"name": alias.name, "kind": alias.kind, "source": alias.source} for alias in pin.aliases.all()],
                "article": {"content": article.content} if article and article.content else None,
            },
        )

    _write_json(temp_dir, "pins.json", rows)


def _export_pins_google_takeout(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export pins as a Google Takeout-compatible CSV file."""
    from urbanlens.dashboard.models.pin.model import Pin

    pins = Pin.objects.filter(profile=profile).select_related("location").prefetch_related("labels").order_by("created")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Title", "Note", "URL", "Tags", "Comment"])

    for pin in pins:
        name = pin.effective_name
        note = pin.description or ""
        url = f"{base_url.rstrip('/')}/dashboard/map/pin/{pin.slug}/" if pin.slug else ""
        tags = ", ".join(b.name for b in pin.labels.all() if hasattr(b, "name"))
        writer.writerow([name, note, url, tags, ""])

    gt_dir = os.path.join(temp_dir, "google_takeout")
    os.makedirs(gt_dir, exist_ok=True)
    pathlib.Path(os.path.join(gt_dir, "pins.csv")).write_text(buf.getvalue(), encoding="utf-8", newline="")


def _export_labels(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export all labels visible to the user, with pin assignments."""
    from urbanlens.dashboard.models.labels.model import Label

    # Export user-owned labels plus global labels that are assigned to the user's pins.
    user_labels = Label.objects.filter(profile=profile).prefetch_related("parents", "pins")
    global_assigned = Label.objects.filter(profile__isnull=True, pins__profile=profile).distinct().prefetch_related("parents", "pins")

    seen: set[int] = set()
    rows = []

    for label in list(user_labels) + list(global_assigned):
        if label.pk in seen:
            continue
        seen.add(label.pk)

        rows.append(
            {
                "uuid": str(label.uuid),
                "name": label.name,
                "description": label.description or "",
                "color": label.color or "",
                "icon": label.icon or "",
                "kind": label.kind,
                "order": label.order,
                "is_user_label": label.profile_id is not None,
                "is_protected": label.is_protected,
                "parent_uuids": [str(p.uuid) for p in label.parents.all()],
                "pin_uuids": [str(p.uuid) for p in label.pins.filter(profile=profile)],
            },
        )

    with open(os.path.join(temp_dir, "labels.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_connections(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export friendship connections as a list of relationship records.

    Outgoing rows the RECIPIENT hasn't accepted (requested/declined/ignored)
    are anonymized - identity nulled and the three states collapsed into one
    ``"pending"`` value. Until a request is accepted, the sender must not be
    able to learn who they reached, whether an invited email belongs to a
    registered account, or how (or whether) the recipient responded - the
    same rule the pending-requests widget enforces (see
    ``controllers.friendship._friend_list_ctx``); an export that included the
    real username/uuid/status would reopen that exact enumeration channel.
    Sender-initiated states (accepted friendships being removed, blocks,
    mutes) keep their identity: the sender necessarily already knows who
    they acted on.
    """
    from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
    from urbanlens.dashboard.models.friendship.model import Friendship

    friendships = Friendship.objects.filter(from_profile=profile).select_related("to_profile__user").order_by("created")
    incoming = Friendship.objects.filter(to_profile=profile).select_related("from_profile__user").order_by("created")

    hidden_outgoing_statuses = {FriendshipStatus.REQUESTED, FriendshipStatus.DECLINED, FriendshipStatus.IGNORED}

    rows = []
    for f in friendships:
        if f.status in hidden_outgoing_statuses:
            rows.append(
                {
                    "other_user_uuid": None,
                    "other_username": None,
                    "status": "pending",
                    "relationship_type": f.relationship_type,
                    "permissions": f.permissions,
                    "direction": "outgoing",
                    "created": str(f.created),
                },
            )
            continue
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


def _export_direct_messages(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export every direct message the user sent or received, one row per message.

    The exporting user's own content (body, attachments, whether they sent or
    received it) is always included in full - "they should always be able to
    see their own messages". The conversation partner's identity is passed
    through `display_identity_for`, the same check the messages page itself
    uses, so an export never reveals a partner's name/avatar beyond what the
    user could currently see on screen (e.g. after being blocked or a privacy
    change). Tombstoned/expired message bodies are also masked per the
    viewer's own tombstone rules, for the same reason.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.services.messaging.direct_messages import display_identity_for

    messages = DirectMessage.objects.involving(profile).select_related("sender", "recipient").prefetch_related("images").order_by("created")

    identity_cache: dict[int, dict[str, Any]] = {}

    def _identity(partner: Any) -> dict[str, Any]:
        if partner.pk not in identity_cache:
            identity_cache[partner.pk] = display_identity_for(profile, partner)
        return identity_cache[partner.pk]

    rows = []
    for message in messages:
        is_sender = message.sender_id == profile.pk
        partner = message.recipient if is_sender else message.sender
        tombstone = message.tombstone_text_for(profile.pk)
        identity = _identity(partner)
        row: dict[str, Any] = {
            "id": message.pk,
            "direction": "sent" if is_sender else "received",
            "partner_display_name": identity["display_name"],
            # Stable identifier for the restore path (sent messages only, see
            # _import_direct_messages). Withheld whenever the partner's
            # identity is masked from the exporter - the uuid would identify
            # them just as surely as their name.
            "partner_uuid": None if identity["is_anonymized"] else str(partner.uuid),
            "is_tombstoned": bool(tombstone),
            "image_count": message.images.count() if not tombstone else 0,
            "has_map": bool(message.markup_map_id) and not tombstone,
            "created": str(message.created),
            "read": message.read_at is not None,
        }
        if tombstone:
            row["body"] = tombstone
        elif message.is_encrypted:
            # End-to-end encrypted: the server has no plaintext. Export the raw
            # ciphertext (only the user's own key can read it) plus a note, and
            # offer an in-browser "download decrypted transcript" on the
            # messages page for a readable copy.
            row["body"] = None
            row["encrypted"] = True
            row["ciphertext"] = message.ciphertext
            row["nonce"] = message.nonce
            row["key_version"] = message.key_version
            row["note"] = "End-to-end encrypted. Decrypt with your account's message key (see the messages page)."
        else:
            row["body"] = message.body
        rows.append(row)

    with open(os.path.join(temp_dir, "direct_messages.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_visit_history(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export all visit history records for the user's pins."""
    from urbanlens.dashboard.models.visits.model import PinVisit

    visits = PinVisit.objects.filter(pin__profile=profile).select_related("pin").order_by("visited_at")

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

    comments = Comment.objects.filter(profile=profile).select_related("pin__location", "wiki").order_by("created")

    rows = []
    for comment in comments:
        target_type, target, target_uuid = _resolve_target(comment)
        rows.append(
            {
                "uuid": str(comment.uuid),
                "target_type": target_type,
                "target_name": target,
                "target_uuid": target_uuid,
                "text": comment.text,
                "created": str(comment.created),
            },
        )

    with open(os.path.join(temp_dir, "comments.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_photos(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    from urbanlens.dashboard.models.images.model import Image

    images = Image.objects.filter(profile=profile).select_related("pin__location", "wiki").prefetch_related("labels").order_by("created")

    photos_dir = os.path.join(temp_dir, "photos")
    os.makedirs(photos_dir, exist_ok=True)

    metadata = []
    for image in images:
        target_type, target, target_uuid = _resolve_target(image)
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
                "media_type": image.media_type,
                "target_type": target_type,
                "target_name": target,
                "target_uuid": target_uuid,
                "latitude": str(image.latitude) if image.latitude else None,
                "longitude": str(image.longitude) if image.longitude else None,
                "created": str(image.created),
                "label_uuids": [str(label.uuid) for label in image.labels.all()],
            },
        )

    with open(os.path.join(photos_dir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False)


def _export_trips(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    """Export the trips this user is a member of.

    Member and creator names go through ``resolve_visible_identities``, the same
    resolution ``services.trips.trip_membership`` applies when the trip page renders
    its member list - so a co-member whose profile visibility hides them from this
    user is "Member 2" in the export exactly as they are on screen. This mirrors
    what :func:`_export_direct_messages` does for a conversation partner: an export
    is a copy of what the user can see, not a way around what they cannot.

    ``member_uuids`` is still exported for everyone, masked or not. It carries no
    name, and the import's re-invite step needs it to rebuild the trip's membership;
    dropping it would turn a privacy fix into a lost feature.
    """
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identities

    trips = Trip.objects.filter(profiles=profile).prefetch_related("profiles__user").select_related("creator__user").order_by("created")

    rows = []
    for trip in trips:
        members = list(trip.profiles.all())
        identities = resolve_visible_identities(profile, members)

        def _name(subject: Any, identities: dict = identities) -> str:
            return identities.get(subject.pk, {}).get("display_name") or subject.username

        rows.append(
            {
                "uuid": str(trip.uuid),
                "name": trip.name,
                "description": trip.description or "",
                "start_date": str(trip.start_date) if trip.start_date else None,
                "end_date": str(trip.end_date) if trip.end_date else None,
                "creator": _name(trip.creator) if trip.creator else None,
                # Whether the exporting user created this trip - the importer
                # only re-creates trips the user owned (a membership in someone
                # else's trip records THEIR trip, which an import can't rebuild
                # on their behalf).
                "is_creator": trip.creator_id == profile.pk,
                "members": [_name(p) for p in members],
                # Stable identifiers for re-inviting members on import; same
                # order as ``members``. Not a name, so exported for masked
                # members too - see this function's docstring.
                "member_uuids": [str(p.uuid) for p in members],
                "created": str(trip.created),
            },
        )

    with open(os.path.join(temp_dir, "trips.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_pin_lists(profile: Any, temp_dir: str, *, base_url: str = "") -> None:
    from urbanlens.dashboard.models.pin_list.model import PinList

    lists = PinList.objects.for_profile(profile).prefetch_related("items__pin").order_by("created")

    rows = []
    for pin_list in lists:
        rows.append(
            {
                "uuid": str(pin_list.uuid),
                "name": pin_list.name,
                "description": pin_list.description or "",
                "is_smart": pin_list.is_smart,
                "smart_filter": pin_list.smart_filter,
                "smart_boundary": json.loads(pin_list.smart_boundary.geojson) if pin_list.smart_boundary else None,
                "created": str(pin_list.created),
                "items": [{"pin_uuid": str(item.pin.uuid), "order": item.order, "added_via": item.added_via} for item in pin_list.items.all()],
            },
        )

    _write_json(temp_dir, "pin_lists.json", rows)


# -- Declarative export types ---------------------------------------------------


class ExportType(ABC):
    """One selectable export area, written into the archive as a single JSON file.

    The original export areas are plain ``_export_*`` functions listed by name in
    :data:`_LEGACY_ORDERED_TYPES` and wired into :func:`run_export` by hand.
    Areas added since subclass this instead, so adding another one means writing
    a class and appending an instance to :data:`_REGISTERED_EXPORT_TYPES` - the
    checkbox allowlist (:data:`VALID_EXPORT_TYPES`), the run order
    (:data:`_ORDERED_TYPES`) and the dispatch table in :func:`run_export` all
    derive from that tuple.

    Instances are callable with the same ``(profile, temp_dir, *, base_url)``
    signature the legacy functions use, so both kinds dispatch identically.

    Attributes:
        key: The value the UI checkbox posts in ``export_types``.
        filename: Path of the JSON file, relative to the archive root.
        message: Progress message shown while this step runs.
        label: Checkbox label on the Tools export card.
        description: One-line hint rendered under ``label``.
    """

    key: ClassVar[str]
    filename: ClassVar[str]
    message: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def payload(self, profile: Any, temp_dir: str, base_url: str) -> Any:
        """Build the JSON-serializable content of :attr:`filename`.

        Args:
            profile: The profile being exported.
            temp_dir: The archive's staging directory - for a type that also
                writes media files alongside its JSON.
            base_url: Absolute site root URL, for types that emit links.

        Returns:
            Any JSON-serializable value.
        """

    def __call__(self, profile: Any, temp_dir: str, *, base_url: str = "") -> None:
        """Run this export step.

        Args:
            profile: The profile being exported.
            temp_dir: The archive's staging directory.
            base_url: Absolute site root URL.
        """
        _write_json(temp_dir, self.filename, self.payload(profile, temp_dir, base_url))


class ModelExportType[ExportedModelT: Model](ExportType):
    """An :class:`ExportType` backed by one profile-scoped queryset, one JSON row per object.

    Covers the common shape: filter the model to the exporting profile, order it
    deterministically, and map each object to a dict. Types spanning several
    models (see :class:`MapAnnotationsExport`) subclass :class:`ExportType`
    directly instead.
    """

    @abstractmethod
    def queryset(self, profile: Any) -> Iterable[ExportedModelT]:
        """Return this profile's rows, deterministically ordered and prefetched.

        Args:
            profile: The profile being exported.

        Returns:
            An iterable of model instances.
        """

    @abstractmethod
    def row(self, obj: ExportedModelT) -> dict[str, Any]:
        """Return the exported dict for one object.

        Args:
            obj: A model instance from :meth:`queryset`.

        Returns:
            A JSON-serializable dict.
        """

    def payload(self, profile: Any, temp_dir: str, base_url: str) -> list[dict[str, Any]]:
        """Return one row per object in :meth:`queryset`.

        Args:
            profile: The profile being exported.
            temp_dir: Unused by the single-queryset shape.
            base_url: Unused by the single-queryset shape.

        Returns:
            The exported rows.
        """
        return [self.row(obj) for obj in self.queryset(profile)]


def _markup_row(item: PinMarkup) -> dict[str, Any]:
    """Return the exported dict for one markup annotation.

    Shared by the map-scoped items nested under each ``MarkupMap`` and the
    standalone pin/wiki-scoped items, so both carry identical fields.

    Args:
        item: The annotation to serialize.

    Returns:
        A JSON-serializable dict. ``layer_name`` is present for readability
        only - custom layers are not themselves exported, so a restored item
        lands on the base markup layer.
    """
    return {
        "uuid": str(item.uuid),
        "markup_type": item.markup_type,
        "geometry": item.geometry,
        "label": item.label or "",
        "color": item.color or "",
        "stroke_width": item.stroke_width,
        "border_color": item.border_color or "",
        "fill_opacity": item.fill_opacity,
        "border_opacity": item.border_opacity,
        "security_indicator": item.security_indicator or "",
        "layer_name": item.layer.name if item.layer else None,
        "created": str(item.created),
    }


class SafetyCheckinsExport(ModelExportType["SafetyCheckin"]):
    """Every safety check-in the user planned, with its contacts and message thread.

    A contact row carries the name/email the user typed and when that contact was
    notified, but never the contact's ``token``: that is the magic-link credential
    for the public contact portal, and an archive the user downloads (and may
    forward) must not contain a working key into their own check-in - the same
    rule that keeps passkeys and message keys out of the archive entirely.

    The shared ``destination_location`` is omitted for the reason ``_export_pins``
    omits ``Location``: it is instance-owned community data, not this user's.
    The destination they actually chose survives as plain coordinates. The route
    map and any attached reference maps are referenced by uuid; their contents
    live in the ``map_annotations`` area.
    """

    key = "safety_checkins"
    filename = "safety_checkins.json"
    message = "Exporting safety check-ins..."
    label = "Safety check-ins"
    description = "Check-ins, their emergency contacts, and messages"

    def queryset(self, profile: Any) -> Iterable[SafetyCheckin]:
        """Return this profile's check-ins, oldest first.

        Args:
            profile: The profile being exported.

        Returns:
            Check-ins with contacts, messages and attached maps prefetched.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckin

        return SafetyCheckin.objects.filter(profile=profile).select_related("trip", "markup_map").prefetch_related("contacts__contact_profile", "messages__sender_profile", "messages__sender_contact", "markup_maps").order_by("created")

    def row(self, obj: SafetyCheckin) -> dict[str, Any]:
        """Return the exported dict for one check-in.

        Args:
            obj: The check-in to serialize.

        Returns:
            A JSON-serializable dict including nested contacts and messages.
        """
        return {
            "uuid": str(obj.uuid),
            "title": obj.title,
            "plan_details": obj.plan_details or "",
            "contact_message": obj.contact_message or "",
            "checkin_by": str(obj.checkin_by) if obj.checkin_by else None,
            "grace_period_seconds": obj.grace_period.total_seconds() if obj.grace_period is not None else None,
            "status": obj.status,
            "destination_latitude": str(obj.destination_latitude) if obj.destination_latitude is not None else None,
            "destination_longitude": str(obj.destination_longitude) if obj.destination_longitude is not None else None,
            "notify_community_wiki": obj.notify_community_wiki,
            "escalated_at": str(obj.escalated_at) if obj.escalated_at else None,
            "resolved_at": str(obj.resolved_at) if obj.resolved_at else None,
            "resolved_by_label": obj.resolved_by_label or "",
            "trip_uuid": str(obj.trip.uuid) if obj.trip else None,
            "markup_map_uuid": str(obj.markup_map.uuid) if obj.markup_map else None,
            "attached_markup_map_uuids": [str(markup_map.uuid) for markup_map in obj.markup_maps.all()],
            "created": str(obj.created),
            "contacts": [
                {
                    "name": contact.name or "",
                    "email": contact.email or "",
                    # The owner picked this person as an emergency contact and
                    # sees them named on their own check-in page, so identity is
                    # not withheld here the way _export_connections withholds an
                    # unaccepted request's recipient.
                    "contact_profile_uuid": str(contact.contact_profile.uuid) if contact.contact_profile else None,
                    "contact_profile_username": contact.contact_profile.username if contact.contact_profile else None,
                    "notified_at": str(contact.notified_at) if contact.notified_at else None,
                    "found_safe_at": str(contact.found_safe_at) if contact.found_safe_at else None,
                    "created": str(contact.created),
                }
                for contact in obj.contacts.all()
            ],
            "messages": [
                {
                    "body": message.body,
                    "sender": "owner" if message.sender_profile_id == obj.profile_id else "contact",
                    "sender_name": message.sender_name,
                    "created": str(message.created),
                }
                for message in obj.messages.all()
            ],
        }


class MapAnnotationsExport(ExportType):
    """Standalone markup maps, the annotations drawn on them, and georeferenced image overlays.

    Three models make one area because they are one feature: a ``MarkupMap`` is
    a saved viewport whose ``PinMarkup`` items are meaningless without it, and a
    ``MapImageOverlay`` is another drawing on the same maps. The payload is
    therefore an object rather than a list:

    * ``maps`` - each ``MarkupMap`` with its own items nested inside it.
    * ``markup`` - annotations drawn directly on a pin's or a wiki's map,
      referenced by ``target_type``/``target_uuid`` the way comments and photos are.
    * ``overlays`` - georeferenced images, with any stored file copied into
      ``map_annotations/`` exactly as ``_export_photos`` copies photos.

    Wiki-scoped markup is included because the user drew it, even though it is
    shared community data - the same reason a received direct message is
    exported. Only their own personal (pin- and map-scoped) markup is restorable
    on import.
    """

    key = "map_annotations"
    filename = "map_annotations.json"
    message = "Exporting map annotations..."
    label = "Map annotations"
    description = "Markup maps, pin markup, and image overlays"

    #: Subdirectory (relative to the archive root) holding overlay image files.
    files_dir_name: ClassVar[str] = "map_annotations"

    def payload(self, profile: Any, temp_dir: str, base_url: str) -> dict[str, Any]:
        """Build the three annotation sections, copying overlay files as it goes.

        Args:
            profile: The profile being exported.
            temp_dir: The archive's staging directory (overlay files land in
                ``<temp_dir>/map_annotations/``).
            base_url: Unused.

        Returns:
            A dict with ``maps``, ``markup`` and ``overlays`` keys.
        """
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
        from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup

        maps = MarkupMap.objects.for_profile(profile).select_related("pin").prefetch_related("items__layer").order_by("created")
        standalone = PinMarkup.objects.for_profile(profile).filter(parent_map__isnull=True).select_related("parent_pin", "parent_wiki", "layer").order_by("created")
        overlays = MapImageOverlay.objects.for_profile(profile).select_related("image", "parent_pin", "parent_wiki", "layer").order_by("created")

        return {
            "maps": [
                {
                    "uuid": str(markup_map.uuid),
                    "title": markup_map.title or "",
                    "center_latitude": markup_map.center_latitude,
                    "center_longitude": markup_map.center_longitude,
                    "zoom": markup_map.zoom,
                    "layer_mode": markup_map.layer_mode,
                    "show_borders": markup_map.show_borders,
                    "pin_uuid": str(markup_map.pin.uuid) if markup_map.pin else None,
                    "created": str(markup_map.created),
                    "items": [_markup_row(item) for item in markup_map.items.all()],
                }
                for markup_map in maps
            ],
            "markup": [{**_markup_row(item), **_annotation_target(item)} for item in standalone],
            "overlays": [self._overlay_row(overlay, temp_dir) for overlay in overlays],
        }

    def _overlay_row(self, overlay: MapImageOverlay, temp_dir: str) -> dict[str, Any]:
        """Return the exported dict for one image overlay, copying its file into the archive.

        Args:
            overlay: The overlay to serialize.
            temp_dir: The archive's staging directory.

        Returns:
            A JSON-serializable dict; ``filename`` is None for an overlay that
            references a remote ``image_url`` rather than a stored file.
        """
        files_dir = os.path.join(temp_dir, self.files_dir_name)
        os.makedirs(files_dir, exist_ok=True)
        stored = overlay.image.image if overlay.image_id and overlay.image and overlay.image.image else None
        filename = _copy_into_archive(stored.path if stored is not None else None, files_dir, overlay.pk)

        return {
            "uuid": str(overlay.uuid),
            "name": overlay.name or "",
            "filename": filename,
            "image_url": overlay.image_url or "",
            "corners": overlay.corners(),
            "opacity": overlay.opacity,
            "order": overlay.order,
            "default_visible": overlay.default_visible,
            "locked": overlay.locked,
            "layer_name": overlay.layer.name if overlay.layer else None,
            "created": str(overlay.created),
            **_annotation_target(overlay),
        }


def _annotation_target(obj: Any) -> dict[str, Any]:
    """Return the ``target_type``/``target_uuid`` pair for a pin- or wiki-parented annotation.

    Uses the same vocabulary :func:`_resolve_target` produces for comments and
    photos ("pin" / "location"), so the importer can resolve all of them through
    one helper.

    Args:
        obj: Any object with ``parent_pin``/``parent_wiki`` FKs.

    Returns:
        A dict with ``target_type`` and ``target_uuid``; both empty when the
        annotation has neither parent.
    """
    if obj.parent_pin_id and obj.parent_pin:
        return {"target_type": "pin", "target_uuid": str(obj.parent_pin.uuid)}
    if obj.parent_wiki_id and obj.parent_wiki:
        return {"target_type": "location", "target_uuid": str(obj.parent_wiki.uuid)}
    return {"target_type": "", "target_uuid": ""}


class SavedFiltersExport(ModelExportType["SavedFilter"]):
    """The user's named, reusable main-map filter combinations.

    ``criteria`` is copied verbatim - it is the normalized search-form payload
    ``services.search.filter_criteria`` replays, and can name labels or custom
    fields by identifier. Reinterpreting it here would duplicate that module's
    knowledge, so the importer copies it back as-is the same way
    ``_import_pin_lists`` handles a smart list's ``smart_filter``.
    """

    key = "saved_filters"
    filename = "saved_filters.json"
    message = "Exporting saved filters..."
    label = "Saved filters"
    description = "Your saved map and search filters"

    def queryset(self, profile: Any) -> Iterable[SavedFilter]:
        """Return this profile's saved filters in display order.

        Args:
            profile: The profile being exported.

        Returns:
            Saved filters ordered by ``order`` then creation time.
        """
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        return SavedFilter.objects.filter(profile=profile).order_by("order", "created")

    def row(self, obj: SavedFilter) -> dict[str, Any]:
        """Return the exported dict for one saved filter.

        Args:
            obj: The saved filter to serialize.

        Returns:
            A JSON-serializable dict.
        """
        return {
            "uuid": str(obj.uuid),
            "name": obj.name,
            "icon": obj.icon or "",
            "color": obj.color or "",
            "opacity": obj.opacity,
            "criteria": obj.criteria or {},
            "order": obj.order,
            "created": str(obj.created),
        }


class RoutesExport(ModelExportType["Route"]):
    """GPS tracks and planned routes the user imported from GPX or Google Takeout.

    ``path`` is exported as GeoJSON - the stored simplified polyline, which is
    all the app itself keeps (raw points are discarded at import time), so the
    archive is not a lossier copy than the live row.
    """

    key = "routes"
    filename = "routes.json"
    message = "Exporting routes..."
    label = "Routes"
    description = "Recorded and imported GPS tracks"

    def queryset(self, profile: Any) -> Iterable[Route]:
        """Return this profile's routes, oldest first.

        Args:
            profile: The profile being exported.

        Returns:
            The profile's routes ordered by creation time.
        """
        from urbanlens.dashboard.models.routes.model import Route

        return Route.objects.for_profile(profile).order_by("created")

    def row(self, obj: Route) -> dict[str, Any]:
        """Return the exported dict for one route.

        Args:
            obj: The route to serialize.

        Returns:
            A JSON-serializable dict with the path as a GeoJSON LineString.
        """
        return {
            "uuid": str(obj.uuid),
            "name": obj.name or "",
            "source": obj.source,
            "source_filename": obj.source_filename or "",
            "path": json.loads(obj.path.geojson) if obj.path else None,
            "raw_point_count": obj.raw_point_count,
            "simplified_point_count": obj.simplified_point_count,
            "distance_meters": obj.distance_meters,
            "elevation_gain_meters": obj.elevation_gain_meters,
            "elevation_loss_meters": obj.elevation_loss_meters,
            "started_at": str(obj.started_at) if obj.started_at else None,
            "ended_at": str(obj.ended_at) if obj.ended_at else None,
            "created": str(obj.created),
        }


# -- Export type registry -------------------------------------------------------

#: Every registry-backed export area, in the order they run (after the legacy
#: ones). Adding an area is: write an :class:`ExportType` subclass, append an
#: instance here, and add its checkbox to the tools page.
_REGISTERED_EXPORT_TYPES: tuple[ExportType, ...] = (
    SafetyCheckinsExport(),
    MapAnnotationsExport(),
    SavedFiltersExport(),
    RoutesExport(),
)

_REGISTERED_EXPORTERS: dict[str, ExportType] = {export_type.key: export_type for export_type in _REGISTERED_EXPORT_TYPES}

#: Everything ``ExportStartView`` will accept in a posted ``export_types`` list.
VALID_EXPORT_TYPES = frozenset(_LEGACY_ORDERED_TYPES) | frozenset(_REGISTERED_EXPORTERS)

#: The order export steps run in; also the order their progress messages appear.
_ORDERED_TYPES: list[str] = [*_LEGACY_ORDERED_TYPES, *_REGISTERED_EXPORTERS]

#: Registry-backed export areas, for rendering their Tools-page checkboxes. The
#: legacy areas in :data:`_LEGACY_ORDERED_TYPES` are still written out by hand in
#: the template; anything added as an :class:`ExportType` appears from this alone.
REGISTERED_EXPORT_TYPES: tuple[ExportType, ...] = _REGISTERED_EXPORT_TYPES
