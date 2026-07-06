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
    update_task_progress(self, current=0, total=1, message="Creating location...")
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
    update_task_progress(self, current=0, total=1, message="Preparing export...")
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
    update_task_progress(self, current=0, total=1, message="Preparing import...")
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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def cleanup_vestigial_assets_task() -> dict[str, int]:
    """Sweep stale import/export artifacts missed by per-job cleanup tasks."""
    from urbanlens.dashboard.services.vestigial_assets import cleanup_vestigial_assets

    result = cleanup_vestigial_assets()
    if result.total < 1:
        logger.debug("No vestigial assets found")
    else:
        logger.info("Vestigial asset cleanup complete: %s", result.as_dict())
    return result.as_dict()


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def rebuild_map_pin_cache(self, profile_id: int) -> int:
    """Rebuild the full root-pin map cache for a profile."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.map_pins import MapPinCache

    logger.info("Rebuilding map pin cache for profile %s", profile_id)
    update_task_progress(self, current=0, total=1, message="Rebuilding map cache...")
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

    update_task_progress(self, current=0, total=1, message="Suggesting location category...")
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

    update_task_progress(self, current=0, total=1, message="Suggesting pin category...")
    pin = Pin.objects.filter(pk=pin_id).select_related("profile").first()
    if pin is None:
        logger.info("Pin %s no longer exists; skipping auto-tagging", pin_id)
        return []
    badges = AutoTagService().suggest_for_pin(pin, apply=True)
    update_task_progress(self, current=1, total=1, message="Pin auto-tagging complete")
    return [b.name for b in badges]


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def prefetch_location_external_data(location_id: int, google_place_id: str | None = None) -> None:
    """Pre-warm LocationCache for a newly created Location.

    Runs Wikipedia and NPS lookups so that the first time a user opens the pin
    detail page the data is already cached.  Also migrates any Google Places
    details already held in the Django request cache into LocationCache so the
    pin detail page can skip the Places Details API call.

    Args:
        location_id: PK of the Location to prefetch data for.
        google_place_id: Optional Google Places place_id already resolved by the
            caller; used to copy existing Django-cache data into LocationCache.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

    location = Location.objects.filter(pk=location_id).first()
    if not location:
        logger.info("prefetch_location_external_data: location %s no longer exists", location_id)
        return

    lat = float(location.latitude or 0)
    lng = float(location.longitude or 0)
    if not lat and not lng:
        return

    # Wikipedia
    if LocationCache.get_fresh(location, "wikipedia") is None:
        try:
            from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

            address_components = {
                "locality": location.locality or "",
                "route": location.route or "",
                "street_number": location.street_number or "",
                "administrative_area_level_1": location.administrative_area_level_1 or "",
            }
            name = location.official_name or location.name or ""
            article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name)
            LocationCache.set(location, "wikipedia", article or {}, query_key=name)
            update_location_name_from_external_sources(
                location,
                extra_candidates=[("wikipedia", (article or {}).get("title"))],
            )
            logger.info("prefetch_location_external_data: cached Wikipedia for location %s", location_id)
        except Exception:
            logger.exception("prefetch_location_external_data: Wikipedia lookup failed for location %s", location_id)

    # NPS (US locations only)
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    state_code = location.administrative_area_level_1 or ""
    if state_code and app_settings.nps_api_key and LocationCache.get_fresh(location, "nps") is None:
        try:
            from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

            park = NPSGateway().find_park_near_location(lat, lng, state_code=state_code, location_name=location.official_name or "")
            LocationCache.set(location, "nps", park or {}, query_key=state_code)
            update_location_name_from_external_sources(
                location,
                extra_candidates=[("nps", (park or {}).get("fullName") or (park or {}).get("name"))],
            )
            logger.info("prefetch_location_external_data: cached NPS for location %s", location_id)
        except Exception:
            logger.exception("prefetch_location_external_data: NPS lookup failed for location %s", location_id)

    # Google Places - migrate from Django request cache into LocationCache so the
    # pin detail page can display it without a fresh API call.
    if google_place_id and LocationCache.get_fresh(location, "google_places") is None:
        try:
            from django.core.cache import cache as django_cache

            place_data = django_cache.get(f"ul_place_details_{google_place_id}")
            if place_data:
                LocationCache.set(location, "google_places", place_data, query_key=google_place_id)
                update_location_name_from_external_sources(
                    location,
                    extra_candidates=[
                        ("google_places", place_data.get("name") if isinstance(place_data, dict) else None),
                    ],
                )
                logger.info(
                    "prefetch_location_external_data: migrated Google Places cache for location %s",
                    location_id,
                )
        except Exception:
            logger.exception(
                "prefetch_location_external_data: Google Places migration failed for location %s",
                location_id,
            )


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def process_image_upload(self, image_id: int) -> bool:
    """Extract image metadata after upload and update the Image row."""
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.images import extract_gps_coords, extract_taken_at
    from urbanlens.dashboard.services.memories.visits import maybe_create_photo_visit

    update_task_progress(self, current=0, total=1, message="Processing image metadata...")
    image = Image.objects.filter(pk=image_id).select_related("pin").first()
    if image is None or not image.image:
        return False
    try:
        with image.image.open("rb") as image_file:
            coords = extract_gps_coords(image_file)
            taken_at = extract_taken_at(image_file)
    except (OSError, ValueError) as exc:
        logger.warning("Image metadata extraction failed for image %s: %s", image_id, exc, exc_info=True)
        return False

    update_fields: dict[str, object] = {}
    if coords:
        lat, lng = coords
        image.latitude = Decimal(str(lat))
        image.longitude = Decimal(str(lng))
        update_fields["latitude"] = image.latitude
        update_fields["longitude"] = image.longitude
    if taken_at:
        image.taken_at = taken_at
        update_fields["taken_at"] = taken_at
    if update_fields:
        Image.objects.filter(pk=image_id).update(**update_fields)

    maybe_create_photo_visit(image)

    update_task_progress(self, current=1, total=1, message="Image metadata processed")
    return True


def _run_database_backup(task=None) -> bool:
    """Run database backup and retention cleanup using current site settings."""
    from urbanlens.core.controllers.backups.db import DatabaseBackup
    from urbanlens.dashboard.models.site_settings import SiteSettings

    site_settings = SiteSettings.get_current()
    if task is not None:
        update_task_progress(task, current=0, total=1, message="Running database backup...")
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
        logger.debug("Scheduled database backup skipped; not due or disabled.")
        update_task_progress(self, current=1, total=1, message="Scheduled backup skipped")
        return False
    return _run_database_backup(self)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def refresh_pin_web_search(self, pin_id: int) -> int:
    """Refresh cached web-search results for a pin detail page."""
    from urllib.parse import urlparse

    from django.core.cache import cache

    from urbanlens.core.cache_keys import make_cache_key
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.services.search import format_search_date, get_search_gateway

    pin = Pin.objects.filter(pk=pin_id).select_related("location").first()
    query = pin.get_unique_search_name(quote_name=True) if pin else None
    if not query:
        return 0
    update_task_progress(self, current=0, total=1, message="Refreshing web search...")
    results = get_search_gateway().search(query)
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
