"""Celery tasks for the dashboard application."""

from __future__ import annotations

import logging

from celery import shared_task

from urbanlens.dashboard.services.celery import update_task_progress
from urbanlens.dashboard.services.locations.creation import LocationCreationService

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def create_location_for_pin(self, pin_id: int) -> int | None:
    """Create or find a shared Location for a pin, then link the pin to it."""
    logger.info("Creating background location for pin %s", pin_id)
    update_task_progress(self, current=0, total=1, message="Creating location…")
    location = LocationCreationService().create_for_pin(pin_id)
    update_task_progress(self, current=1, total=1, message="Location ready")
    if location:
        logger.info("Created/linked location %s for pin %s", location.pk, pin_id)
        return location.pk
    logger.info("No location created for pin %s", pin_id)
    return None


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def run_user_data_export(self, user_id: int, export_types: list[str], export_dir: str, base_url: str, job_id: str | None = None) -> bool:
    """Build a user's data export archive outside the web request."""
    from urbanlens.dashboard.services.export import run_export

    logger.info("Starting data export for user %s", user_id)
    update_task_progress(self, current=0, total=1, message="Preparing export…")
    success = run_export(user_id, export_types, export_dir, base_url, job_id=job_id)
    if success:
        update_task_progress(self, current=1, total=1, message="Export ready")
        logger.info("Finished data export for user %s", user_id)
        return True
    update_task_progress(self, current=1, total=1, message="Export failed")
    logger.warning("Data export failed for user %s", user_id)
    return False


@shared_task
def cleanup_export_artifacts_task(export_dir: str, job_id: str | None = None) -> None:
    """Remove expired export artifacts and cache-backed status."""
    from urbanlens.dashboard.services.export import ExportJobStatus, cleanup_export_artifacts

    cleanup_export_artifacts(export_dir, ExportJobStatus(job_id) if job_id else None)
    logger.info("Cleaned up export artifacts for job %s", job_id or export_dir)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def run_user_data_import(self, user_id: int, zip_path: str, job_id: str) -> bool:
    """Parse a UrbanLens export ZIP and import data for the user."""
    from urbanlens.dashboard.services.import_data import run_import

    logger.info("Starting data import for user %s, job %s", user_id, job_id)
    update_task_progress(self, current=0, total=1, message="Preparing import…")
    success = run_import(user_id, zip_path, job_id)
    if success:
        update_task_progress(self, current=1, total=1, message="Import complete")
        logger.info("Finished data import for user %s, job %s", user_id, job_id)
        return True
    update_task_progress(self, current=1, total=1, message="Import failed")
    logger.warning("Data import failed for user %s, job %s", user_id, job_id)
    return False


@shared_task
def cleanup_import_artifacts_task(import_dir_path: str, job_id: str | None = None) -> None:
    """Remove expired import artifacts and cache-backed status."""
    from urbanlens.dashboard.services.import_data import ImportJobStatus, cleanup_import_artifacts

    cleanup_import_artifacts(import_dir_path, ImportJobStatus(job_id) if job_id else None)
    logger.info("Cleaned up import artifacts for job %s", job_id or import_dir_path)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def rebuild_map_pin_cache(self, profile_id: int) -> int:
    """Rebuild the full root-pin map cache for a profile."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.map_pins import MapPinCache

    logger.info("Rebuilding map pin cache for profile %s", profile_id)
    update_task_progress(self, current=0, total=1, message="Rebuilding map cache…")
    profile = Profile.objects.get(pk=profile_id)
    query = Pin.objects.filter(profile=profile).root_pins().select_related("location")
    cache = MapPinCache(profile)
    cache.rebuild(query)
    update_task_progress(self, current=1, total=1, message="Map cache ready")
    return query.count()


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def suggest_location_category(self, location_id: int) -> list[str]:
    """Suggest and attach badges for a Location outside model signals."""
    from urbanlens.dashboard.models.location import Location
    from urbanlens.dashboard.services.auto_tag import AutoTagService

    update_task_progress(self, current=0, total=1, message="Suggesting location category…")
    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("Location %s no longer exists; skipping auto-tagging", location_id)
        return []
    badges = AutoTagService().suggest_for_location(location, apply=True)
    update_task_progress(self, current=1, total=1, message="Location auto-tagging complete")
    return [b.name for b in badges]


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def suggest_pin_category(self, pin_id: int) -> list[str]:
    """Suggest and attach badges for a Pin outside request/import loops."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.auto_tag import AutoTagService

    update_task_progress(self, current=0, total=1, message="Suggesting pin category…")
    pin = Pin.objects.filter(pk=pin_id).select_related("profile").first()
    if pin is None:
        logger.info("Pin %s no longer exists; skipping auto-tagging", pin_id)
        return []
    badges = AutoTagService().suggest_for_pin(pin, apply=True)
    update_task_progress(self, current=1, total=1, message="Pin auto-tagging complete")
    return [b.name for b in badges]


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def process_image_upload(self, image_id: int) -> bool:
    """Extract image metadata after upload and update the Image row."""
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.images import extract_gps_coords

    update_task_progress(self, current=0, total=1, message="Processing image metadata…")
    image = Image.objects.filter(pk=image_id).first()
    if image is None or not image.image:
        return False
    try:
        with image.image.open("rb") as image_file:
            coords = extract_gps_coords(image_file)
    except (OSError, ValueError) as exc:
        logger.warning("Image metadata extraction failed for image %s: %s", image_id, exc, exc_info=True)
        return False
    if coords:
        lat, lng = coords
        Image.objects.filter(pk=image_id).update(latitude=Decimal(str(lat)), longitude=Decimal(str(lng)))
    update_task_progress(self, current=1, total=1, message="Image metadata processed")
    return True


def _run_database_backup(task=None) -> bool:
    """Run database backup and retention cleanup using current site settings."""
    from urbanlens.core.controllers.backups.db import DatabaseBackup
    from urbanlens.dashboard.models.site_settings import SiteSettings

    site_settings = SiteSettings.get_current()
    if task is not None:
        update_task_progress(task, current=0, total=1, message="Running database backup…")
    backup = DatabaseBackup(auto_schedule=False)
    backup.backup_retention = site_settings.backup_retention
    backup.create_backup_dir()
    result = backup.run()
    if task is not None:
        update_task_progress(task, current=1, total=1, message="Database backup complete" if result else "Database backup failed")
    return result


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def run_database_backup(self) -> bool:
    """Run database backup and retention cleanup from a Celery worker."""
    return _run_database_backup(self)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def run_scheduled_database_backup(self) -> bool:
    """Run a database backup only when site-admin schedule settings say it is due."""
    from urbanlens.dashboard.services.backups import scheduled_backup_due

    if not scheduled_backup_due():
        logger.info("Scheduled database backup skipped; not due or disabled.")
        update_task_progress(self, current=1, total=1, message="Scheduled backup skipped")
        return False
    return _run_database_backup(self)


@shared_task(bind=True)
def apply_admin_code_update(self) -> dict:
    """Pull code, apply migrations, and request app reload from Celery."""
    from urbanlens.core.version import (
        apply_pending_migrations,
        get_current_git_commit,
        pull_latest_git_code,
        trigger_development_app_reload,
    )

    update_task_progress(self, current=0, total=3, message="Pulling latest code…")
    before_commit = get_current_git_commit()
    ok, message = pull_latest_git_code()
    after_commit = get_current_git_commit()
    if not ok:
        return {"ok": False, "message": message}

    changed = bool(before_commit and after_commit and before_commit != after_commit)
    migration_message = "Database migrations were not needed because the code was already up to date."
    reload_message = "Development server reload was not needed because the code was already up to date."

    update_task_progress(self, current=1, total=3, message="Applying migrations…")
    if changed:
        migration_ok, migration_message = apply_pending_migrations()
        if not migration_ok:
            return {"ok": False, "message": migration_message, "details": message}

    update_task_progress(self, current=2, total=3, message="Reloading app…")
    if changed:
        reload_ok, reload_message = trigger_development_app_reload()
        if not reload_ok:
            return {"ok": False, "message": reload_message, "details": message}

    update_task_progress(self, current=3, total=3, message="Update complete")
    return {
        "ok": True,
        "changed": changed,
        "message": "Code updated, migrations applied, and app reload requested." if changed else "Code is already up to date.",
        "details": message,
        "migration_details": migration_message,
        "reload_details": reload_message,
    }


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def refresh_pin_web_search(self, pin_id: int) -> int:
    """Refresh cached web-search results for a pin detail page."""
    from urllib.parse import urlparse

    from django.core.cache import cache

    from urbanlens.core.cache_keys import make_cache_key
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.services.search import build_pin_search_query, format_search_date, get_search_gateway

    pin = Pin.objects.filter(pk=pin_id).select_related("location").first()
    if pin is None or not pin.meaningful_name:
        return 0
    update_task_progress(self, current=0, total=1, message="Refreshing web search…")
    results = get_search_gateway().search(build_pin_search_query(pin))
    for result in results:
        try:
            result["domain"] = urlparse(result.get("link", "")).netloc.removeprefix("www.")
        except (ValueError, AttributeError):
            result["domain"] = ""
        result["date_display"] = format_search_date(result.get("date"))
    cache_hours = SiteSettings.get_current().search_cache_hours
    if cache_hours > 0:
        cache.set(make_cache_key("web_search_pin", str(pin.pk)), results, cache_hours * 3600)
    update_task_progress(self, current=1, total=1, message="Web search refreshed")
    return len(results)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def refresh_smithsonian_images(self, pin_id: int) -> int:
    """Warm Smithsonian image cache for a pin."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.smithsonian import SmithsonianGateway
    from urbanlens.UrbanLens.settings.app import settings

    pin = Pin.objects.filter(pk=pin_id).select_related("location").first()
    if pin is None or not pin.meaningful_name:
        return 0
    update_task_progress(self, current=0, total=1, message="Refreshing Smithsonian images…")
    images = [img for img in SmithsonianGateway(api_key=settings.smithsonian_api_key or "").get_data(pin.effective_name) if img.get("url")]
    update_task_progress(self, current=1, total=1, message="Smithsonian images refreshed")
    return len(images)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def refresh_weather_forecast(self, pin_id: int) -> bool:
    """Warm weather forecast provider/cache path for a pin."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.openweather.gateway import WeatherForecastGateway

    pin = Pin.objects.filter(pk=pin_id).first()
    if pin is None or not pin.latitude or not pin.longitude:
        return False
    update_task_progress(self, current=0, total=1, message="Refreshing weather forecast…")
    WeatherForecastGateway().get_weather_forecast(pin.latitude, pin.longitude)
    update_task_progress(self, current=1, total=1, message="Weather forecast refreshed")
    return True


@shared_task(bind=True)
def extract_import_archive(self, archive_path: str, output_dir: str) -> list[str]:
    """Extract and validate a large import archive outside the request."""
    from pathlib import Path

    from urbanlens.dashboard.services.archive_extractor import extract_archive, validate_content_type

    update_task_progress(self, current=0, total=1, message="Extracting import archive…")
    raw = Path(archive_path).read_bytes()
    extracted = extract_archive(raw)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    total = max(len(extracted), 1)
    for index, item in enumerate(extracted, 1):
        if validate_content_type(item.name, item.data) is None:
            continue
        output_path = Path(output_dir, item.name).resolve()
        if Path(output_dir).resolve() not in output_path.parents:
            continue
        output_path.write_bytes(item.data)
        written.append(str(output_path))
        update_task_progress(self, current=index, total=total, message=f"Extracted {index} of {total} files…")
    update_task_progress(self, current=total, total=total, message="Archive extraction complete")
    return written


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def import_pins_from_paths(self, profile_id: int, files: list[tuple[str, str]], tag_ids: list[int] | None = None, tag_by_filename: bool = False) -> dict:
    """Import pins from uploaded files stored on disk and report Celery progress."""
    import json
    from pathlib import Path

    from django.conf import settings as django_settings

    from urbanlens.dashboard.models.badges.model import Badge
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.google.maps import GoogleMapsGateway
    from urbanlens.UrbanLens.settings.app import settings

    media_root = Path(django_settings.MEDIA_ROOT).resolve()
    profile = Profile.objects.get(pk=profile_id)
    raw_files = []
    for name, path in files:
        source_path = Path(path).resolve()
        if media_root not in source_path.parents and source_path != media_root:
            logger.warning("Skipping import file outside MEDIA_ROOT: %s", source_path)
            continue
        raw_files.append((name, source_path.read_bytes()))
    tags = list(Badge.objects.filter(pk__in=tag_ids or []))
    gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")
    summary: dict = {"created": 0, "exists": 0, "skipped": 0, "total": 0}
    for event in gateway.import_pins_streaming(raw_files, profile, tags=tags, tag_by_filename=tag_by_filename):
        payload = json.loads(event.removeprefix("data: ").strip())
        if payload.get("type") == "start":
            summary["total"] = payload.get("total", 0)
            update_task_progress(self, current=0, total=max(summary["total"], 1), message="Import started…")
        elif payload.get("type") == "progress":
            summary.update({k: payload.get(k, summary.get(k, 0)) for k in ("created", "exists", "skipped", "total")})
            update_task_progress(self, current=payload.get("current", 0), total=max(payload.get("total", 1), 1), message=f"Importing {payload.get('name', '')}".strip())
        elif payload.get("type") == "complete":
            summary.update({k: payload.get(k, summary.get(k, 0)) for k in ("created", "exists", "skipped", "total")})
    return summary
