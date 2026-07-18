"""Celery tasks for the dashboard application."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from celery import shared_task

from urbanlens.dashboard.services.celery import update_task_progress

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def enrich_wiki_location(self, wiki_id: int) -> bool:
    """Enrich a freshly user-created Wiki's Location with external data.

    Runs after the user clicks "Create community wiki": links the Location to
    its Google Place, resolves a canonical name when the wiki is still
    unnamed, and generates the location's default property/building
    boundaries. This is the only place these APIs are hit for a new wiki -
    pin creation and bulk imports do no external work.

    Args:
        wiki_id: PK of the newly created Wiki.

    Returns:
        True when the wiki still existed and enrichment ran.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService
    from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, generate_location_boundaries
    from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain

    wiki = Wiki.objects.select_related("location").filter(pk=wiki_id).first()
    if wiki is None or wiki.location_id is None:
        logger.info("enrich_wiki_location: wiki %s no longer exists or has no location", wiki_id)
        return False

    location = wiki.location
    update_task_progress(self, current=0, total=2, message="Resolving place details...")

    name_resolver = PlaceNameResolverChain()
    try:
        if location.google_place_id is None:
            GooglePlaceService(name_resolver=name_resolver).ensure_linked(location)
    except Exception:
        logger.exception("enrich_wiki_location: Google place linking failed for location %s", location.pk)

    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    if not is_meaningful_name(wiki.name):
        from urbanlens.dashboard.services.locations.naming import sanitize_name

        try:
            place_name = location.official_name or name_resolver.resolve(float(location.latitude), float(location.longitude))
        except Exception:
            logger.exception("enrich_wiki_location: name resolution failed for location %s", location.pk)
            place_name = None
        # This bypasses Wiki.save() (a bulk .update()), so sanitize here too -
        # location.official_name is already sanitized by Location.save(), but
        # name_resolver.resolve() is a live external-source result that isn't.
        # The name= filter re-checks the wiki still carries the exact
        # non-meaningful name read above (atomically, in the same query), so a
        # concurrent user-driven rename isn't clobbered. Filtering on the name
        # actually read - rather than reconstructing the set of possible
        # placeholders - also can't drift out of sync with whatever variant
        # was seeded: an area-suffixed placeholder built from an OLDER
        # area_label (the address backfill may have changed it since),
        # a coordinate-style name, or any future placeholder shape all pass
        # the is_meaningful_name gate above and match here.
        if place_name := sanitize_name(place_name):
            Wiki.objects.filter(pk=wiki.pk, name=wiki.name).update(name=place_name)

    update_task_progress(self, current=1, total=2, message="Generating boundaries...")
    if not boundary_generation_ran(location):
        generate_location_boundaries(location, name=wiki.name or None)

    update_task_progress(self, current=2, total=2, message="Wiki ready")
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def generate_boundaries_for_location(location_id: int) -> bool:
    """Generate the default property/building boundaries for a Location.

    Scheduled single-flight by ``schedule_location_boundary_generation`` (wiki
    page) - the pin detail page uses the "boundary" panel source instead, which
    calls the same ``generate_location_boundaries`` function.

    Args:
        location_id: PK of the Location.

    Returns:
        True when the location existed and generation ran (or had already run).
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, generate_location_boundaries

    try:
        location = Location.objects.filter(pk=location_id).first()
        if location is None:
            logger.info("generate_boundaries_for_location: location %s no longer exists", location_id)
            return False
        if not boundary_generation_ran(location):
            generate_location_boundaries(location)
        return True
    finally:
        cache.delete(f"ul_boundary_generation_{location_id}")


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def warm_saved_filter_cache(profile_id: int) -> int:
    """Precompute and cache a profile's saved-filter matching-pin uuid lists.

    Queued right after login (see ``models.profile.signals``) so the bottom-right
    map toolbar's first filter toggle of the session hits a warm
    ``services.saved_filter_cache`` entry instead of a cold query.

    Args:
        profile_id: PK of the ``Profile`` to warm - never a bare user-supplied
            uuid, so this can't be used to warm (or probe) another user's data.

    Returns:
        Number of saved filters warmed, or 0 if the profile no longer exists.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.saved_filter_cache import warm_all_for_profile

    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        return 0
    return warm_all_for_profile(profile)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def push_trip_to_calendar(trip_id: int) -> int:
    """Push a trip's current state to every calendar it is auto-synced with.

    Queued after a trip or trip activity is saved, so calendar events created
    by the "keep in sync" import option stay current without the user having
    to re-export manually. Sync is one-way (UrbanLens to Google) only.

    Args:
        trip_id: PK of the trip that changed.

    Returns:
        The number of calendars the trip was successfully pushed to.
    """
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.services.calendar_sync import push_auto_synced_trip_changes

    trip = Trip.objects.filter(pk=trip_id).first()
    if trip is None:
        logger.info("push_trip_to_calendar: trip %s no longer exists", trip_id)
        return 0
    return push_auto_synced_trip_changes(trip)


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
def suggest_wiki_category(self, wiki_id: int) -> list[str]:
    """Suggest and attach labels for a community Wiki outside model signals."""
    from urbanlens.dashboard.models.wiki import Wiki
    from urbanlens.dashboard.services.auto_tag import AutoTagService

    update_task_progress(self, current=0, total=1, message="Suggesting wiki category...")
    wiki = Wiki.objects.filter(pk=wiki_id).select_related("location").first()
    if wiki is None:
        logger.info("Wiki %s no longer exists; skipping auto-tagging", wiki_id)
        return []
    labels = AutoTagService().suggest_for_wiki(wiki, apply=True)
    update_task_progress(self, current=1, total=1, message="Wiki auto-tagging complete")
    return [b.name for b in labels]


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def suggest_pin_category(self, pin_id: int) -> list[str]:
    """Suggest and attach labels for a Pin outside request/import loops."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.auto_tag import AutoTagService

    update_task_progress(self, current=0, total=1, message="Suggesting pin category...")
    pin = Pin.objects.filter(pk=pin_id).select_related("profile").first()
    if pin is None:
        logger.info("Pin %s no longer exists; skipping auto-tagging", pin_id)
        return []
    labels = AutoTagService().suggest_for_pin(pin, apply=True)
    update_task_progress(self, current=1, total=1, message="Pin auto-tagging complete")
    return [b.name for b in labels]


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def resolve_location_place_name(location_id: int) -> str | None:
    """Fetch and cache a Location's Google place name outside the request/response cycle.

    Location.place_name is deliberately cache-only (see its docstring) - this
    is what actually populates that cache, dispatched from wherever a missing
    place name is first noticed (e.g. PinController.view) so the next render
    of this Location, by any pin/user sharing its coordinates, finds it warm.
    """
    from urbanlens.dashboard.models.location.model import Location

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("resolve_location_place_name: location %s no longer exists", location_id)
        return None
    return location.get_place_name()


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def backfill_location_address(location_id: int) -> bool:
    """Reverse-geocode and persist a Location's street address outside the request/response cycle.

    The background counterpart to ``resolve_location_place_name`` for address
    components: ``ensure_location_address`` makes a live Google Geocoding
    call, so it must never run inline on a page render - PinOverviewView
    dispatches this instead when it notices a route-less location, and the
    next render (by any pin/user sharing this Location) reads the backfilled
    row straight from the DB.

    Args:
        location_id: PK of the Location to backfill.

    Returns:
        True when at least one address component was written.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.addresses import ensure_location_address

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("backfill_location_address: location %s no longer exists", location_id)
        return False
    return ensure_location_address(location)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def archive_link_to_wayback(link_model: str, link_id: int) -> bool:
    """Best-effort archive a PinLink's or WikiLink's URL to the Wayback Machine.

    Prefers an existing recent snapshot (cheap availability check) over asking
    the Wayback Machine to crawl the page again. HTTP-level failures (dead
    link, the Archive refusing the URL, ...) are logged and left for the user
    to retry later rather than retried automatically - only transport-level
    errors (OSError) get Celery's automatic retry, since a permanently
    unarchivable URL would otherwise retry forever.

    Args:
        link_model: ``"PinLink"`` or ``"WikiLink"``.
        link_id: PK of the link row to archive.

    Returns:
        True when a wayback_url was saved, False otherwise.
    """
    import requests

    from urbanlens.dashboard.models.links.model import PinLink, WikiLink
    from urbanlens.dashboard.services.apis.locations.wayback_machine import WaybackMachineGateway

    model = {"PinLink": PinLink, "WikiLink": WikiLink}.get(link_model)
    if model is None:
        logger.warning("archive_link_to_wayback: unknown link_model %r", link_model)
        return False

    link = model.objects.filter(pk=link_id).first()
    if link is None or link.wayback_url:
        return False

    gateway = WaybackMachineGateway()
    try:
        availability = gateway.get_availability(link.url)
        wayback_url = (availability.get("archived_snapshots") or {}).get("closest", {}).get("url", "")
        if not wayback_url:
            saved = gateway.save_url(link.url)
            wayback_url = saved.get("archived_url", "")
    except requests.RequestException:
        logger.warning("archive_link_to_wayback: could not archive %s", link.url, exc_info=True)
        return False

    if not wayback_url:
        return False

    link.wayback_url = wayback_url
    link.save(update_fields=["wayback_url", "updated"])
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def prefetch_location_external_data(location_id: int, google_place_id: str | None = None, profile_id: int | None = None) -> None:
    """Pre-warm LocationCache for a newly created Location.

    Runs Wikipedia and NPS lookups so that the first time a user opens the pin
    detail page the data is already cached.  Also migrates any Google Places
    details already held in the Django request cache into LocationCache so the
    pin detail page can skip the Places Details API call.

    Args:
        location_id: PK of the Location to prefetch data for.
        google_place_id: Optional Google Places place_id already resolved by the
            caller; used to copy existing Django-cache data into LocationCache.
        profile_id: PK of the profile whose action enqueued this task, if any -
            used to honor that profile's name-source priority override.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

    location = Location.objects.filter(pk=location_id).first()
    if not location:
        logger.info("prefetch_location_external_data: location %s no longer exists", location_id)
        return

    profile = Profile.objects.filter(pk=profile_id).first() if profile_id else None

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
            name = location.official_name or location.display_name or ""
            article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name)
            LocationCache.set(location, "wikipedia", article or {}, query_key=name)
            logger.info("prefetch_location_external_data: cached Wikipedia for location %s", location_id)
        except Exception:
            logger.exception("prefetch_location_external_data: Wikipedia lookup failed for location %s", location_id)

    # NPS: caches the park the location sits inside, if any. Non-US coordinates
    # are filtered out (before any network call) by find_park_containing_location.
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if app_settings.nps_api_key and LocationCache.get_fresh(location, "nps") is None:
        try:
            from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

            park = NPSGateway().find_park_containing_location(lat, lng)
            LocationCache.set(location, "nps", park or {}, query_key=f"{lat:.5f},{lng:.5f}")
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
                logger.info(
                    "prefetch_location_external_data: migrated Google Places cache for location %s",
                    location_id,
                )
        except Exception:
            logger.exception(
                "prefetch_location_external_data: Google Places migration failed for location %s",
                location_id,
            )

    # Resolve the official name once, after every cache write above has landed,
    # so the plugin name providers see all fresh candidates in a single pass
    # (per-source refreshes let whichever source ran last win).
    try:
        update_location_name_from_external_sources(location, profile=profile)
    except Exception:
        logger.exception("prefetch_location_external_data: name refresh failed for location %s", location_id)


@dataclass
class _UploadProcessResult:
    """What each media-type-specific processing step produced."""

    update_fields: dict[str, object]
    coords: tuple[float, float] | None = None
    new_stored_size: int | None = None


def _process_photo_upload(image: Image, image_id: int, strip_location: bool) -> _UploadProcessResult | None:
    """Photo-specific metadata extraction and downscaling.

    Returns None on unrecoverable read failure (the caller treats that as a
    failed task run).
    """
    from urbanlens.dashboard.services.images import (
        compute_checksum,
        downscale_stored_image,
        extract_author,
        extract_caption_from_metadata,
        extract_copyright_notice,
        extract_exif_data,
        extract_gps_coords,
        extract_source_url,
        extract_taken_at,
        is_camera_generated_filename,
    )
    from urbanlens.dashboard.services.storage import get_downscale_policy

    try:
        with image.image.open("rb") as image_file:
            coords = None if strip_location else extract_gps_coords(image_file)
            taken_at = extract_taken_at(image_file)
            checksum = compute_checksum(image_file) if not image.checksum else None
            exif_data = extract_exif_data(image_file) if image.exif_data is None else None
            author = extract_author(image_file) if not image.author else None
            copyright_notice = extract_copyright_notice(image_file) if not image.copyright else None
            metadata_caption = extract_caption_from_metadata(image_file) if not image.caption else None
            source_url = extract_source_url(image_file) if not image.source_url else None
    except (OSError, ValueError) as exc:
        logger.warning("Image metadata extraction failed for image %s: %s", image_id, exc, exc_info=True)
        return None

    if strip_location and exif_data:
        exif_data.pop("GPSInfo", None)

    update_fields: dict[str, object] = {}
    if taken_at:
        image.taken_at = taken_at
        update_fields["taken_at"] = taken_at
    if checksum:
        image.checksum = checksum
        update_fields["checksum"] = checksum
    if exif_data:
        image.exif_data = exif_data
        update_fields["exif_data"] = exif_data
    if author:
        image.author = author
        update_fields["author"] = author
    if copyright_notice:
        image.copyright = copyright_notice
        update_fields["copyright"] = copyright_notice
    if metadata_caption:
        image.caption = metadata_caption
        update_fields["caption"] = metadata_caption
    if source_url:
        image.source_url = source_url
        update_fields["source_url"] = source_url

    if image.profile is not None and not (image.author or image.source_url or image.caption or image.copyright) and is_camera_generated_filename(image.image.name or ""):
        uploader_name = image.profile.full_name or image.profile.username
        if uploader_name:
            image.author = uploader_name
            update_fields["author"] = uploader_name

    new_stored_size: int | None = None
    if image.profile is not None:
        max_dimension, convert_webp = get_downscale_policy(image.profile)
        if max_dimension is not None or convert_webp or strip_location:
            try:
                new_size = downscale_stored_image(image, max_dimension, convert_webp, strip_gps=strip_location)
            except (OSError, ValueError) as exc:
                logger.warning("Downscaling failed for image %s: %s", image_id, exc, exc_info=True)
            else:
                if new_size is not None:
                    update_fields["image"] = image.image.name
                    new_stored_size = new_size

    return _UploadProcessResult(update_fields, coords, new_stored_size)


def _process_video_upload(image: Image, strip_location: bool) -> _UploadProcessResult:
    """Video-specific metadata extraction (via ffprobe) and downscaling (via ffmpeg)."""
    from urbanlens.dashboard.services.storage import get_video_downscale_policy
    from urbanlens.dashboard.services.videos import process_uploaded_video

    max_height = get_video_downscale_policy(image.profile) if image.profile is not None else None
    metadata, new_size = process_uploaded_video(image, None if strip_location else max_height)

    update_fields: dict[str, object] = {}
    coords: tuple[float, float] | None = None
    if not strip_location:
        if "taken_at" in metadata:
            image.taken_at = metadata["taken_at"]
            update_fields["taken_at"] = image.taken_at
        if "latitude" in metadata and "longitude" in metadata:
            coords = (metadata["latitude"], metadata["longitude"])
    if new_size is not None:
        update_fields["image"] = image.image.name
    return _UploadProcessResult(update_fields, coords, new_size)


def _process_document_upload(image: Image, image_id: int) -> _UploadProcessResult:
    """Document-specific PDF conversion and OCR text extraction."""
    from urbanlens.dashboard.services.documents import convert_to_pdf, extract_pdf_text

    update_fields: dict[str, object] = {}
    try:
        new_size = convert_to_pdf(image)
    except (OSError, ValueError) as exc:
        logger.warning("Document conversion failed for image %s: %s", image_id, exc, exc_info=True)
        new_size = None
    if new_size is not None:
        update_fields["image"] = image.image.name

    ocr_text = extract_pdf_text(image)
    if ocr_text:
        image.ocr_text = ocr_text
        update_fields["ocr_text"] = ocr_text
    return _UploadProcessResult(update_fields, None, new_size)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def process_image_upload(self, image_id: int) -> bool:
    """Extract metadata after an upload and update the Image row.

    Dispatches to media-type-specific extraction/downscaling (photo: EXIF +
    Pillow; video: ffprobe/ffmpeg; document: LibreOffice-to-PDF + OCR), then
    runs the shared tail identical for every type: resolving the photo's
    ``location`` link (taken from the pin/wiki it's attached to, or resolved
    from GPS via ``get_nearby_or_create``), raising a visit suggestion, and
    queuing keyword generation. This is the single place PinSuggestion/
    VisitSuggestion creation happens for any uploaded media - see
    ``maybe_suggest_photo_visit``.

    Attribution fields (author/source_url/caption/copyright), where
    applicable, are filled from metadata when present and not already set.

    When the uploader has turned off visit-history tracking (``track_pin_visits``),
    GPS is treated as sensitive rather than useful: it's never read into
    ``Image.latitude``/``longitude`` or the ``exif_data`` snapshot, the stored
    file's own embedded GPS tag is stripped where supported, and no visit
    suggestion is raised.
    """
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit
    from urbanlens.dashboard.services.visits import visit_logging_allowed

    update_task_progress(self, current=0, total=1, message="Processing upload metadata...")
    image = Image.objects.filter(pk=image_id).select_related("pin__location", "wiki__location", "profile").first()
    if image is None or not image.image:
        return False

    # A profile with visit-history tracking off doesn't want its location
    # trail reconstructible from any uploaded media either - GPS coordinates
    # are neither extracted into the DB nor left embedded in the stored file
    # below, and no visit suggestion is raised.
    strip_location = image.profile is not None and not visit_logging_allowed(image.profile)

    stored_size: int | None = None
    with contextlib.suppress(OSError):
        stored_size = image.image.size

    if image.media_type == MediaKind.VIDEO:
        result = _process_video_upload(image, strip_location)
    elif image.media_type == MediaKind.DOCUMENT:
        result = _process_document_upload(image, image_id)
    else:
        photo_result = _process_photo_upload(image, image_id, strip_location)
        if photo_result is None:
            return False
        result = photo_result

    update_fields, coords = result.update_fields, result.coords

    if coords:
        lat, lng = coords
        image.latitude = Decimal(str(lat))
        image.longitude = Decimal(str(lng))
        update_fields["latitude"] = image.latitude
        update_fields["longitude"] = image.longitude

    if result.new_stored_size is not None:
        stored_size = result.new_stored_size
    if stored_size is not None and stored_size != image.file_size:
        image.file_size = stored_size
        update_fields["file_size"] = stored_size

    if image.location_id is None:
        location = _resolve_image_location(image, coords)
        if location is not None:
            image.location = location
            update_fields["location"] = location

    if update_fields:
        Image.objects.filter(pk=image_id).update(**update_fields)

    if not strip_location:
        maybe_suggest_photo_visit(image)

    # Keyword generation runs as its own task so a slow provider (AI vision,
    # classifiers) never delays the metadata/downscale pipeline above; it also
    # deliberately runs after the downscale so providers read the final file.
    # Photo-keyword plugins are built around analyzing a raster image, so this
    # only applies to actual photos - videos/documents are made searchable via
    # their own metadata/ocr_text instead.
    if image.media_type == MediaKind.PHOTO:
        from urbanlens.dashboard.services.celery import safely_enqueue_task as _enqueue

        if image.profile is None or image.profile.generate_photo_keywords:
            _enqueue(generate_image_keywords, image_id)

    update_task_progress(self, current=1, total=1, message="Upload metadata processed")
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def generate_image_keywords(image_id: int) -> dict[str, int]:
    """Generate searchable keywords for an uploaded photo via keyword plugins.

    Enqueued at the end of ``process_image_upload`` (fully in the background -
    uploads never wait on it). Each enabled photo-keyword provider stores its
    own ``ImageKeyword`` rows; see ``services.photo_keywords``.

    Args:
        image_id: PK of the image to keyword.

    Returns:
        Mapping of provider slug to keywords stored.
    """
    from urbanlens.dashboard.services.photo_keywords import generate_keywords_for_image

    return generate_keywords_for_image(image_id)


def _resolve_image_location(image: Image, coords: tuple[float, float] | None) -> Location | None:
    """Resolve the shared Location an image belongs to, if determinable.

    Prefers the Location of the pin or wiki the photo is attached to; otherwise
    falls back to matching/creating a Location at the photo's GPS coordinates.

    Args:
        image: The Image needing a location link.
        coords: (latitude, longitude) extracted from EXIF, or None.

    Returns:
        The resolved Location, or None when nothing places the photo.
    """
    from urbanlens.dashboard.models.location.model import Location

    if image.pin is not None and image.pin.location_id is not None:
        return image.pin.location
    if image.wiki is not None and image.wiki.location_id is not None:
        return image.wiki.location
    if coords:
        lat, lng = coords
        location, _created = Location.objects.get_nearby_or_create(lat, lng)
        return location
    return None


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def import_immich_photos(self, pin_id: int, profile_id: int, asset_ids: list[str], visit_id_by_asset: dict[str, int] | None = None) -> dict[str, int]:
    """Download selected Immich assets and import them onto a pin.

    Runs the same checksum-dedupe and storage-quota checks as a manual upload
    (``PinGalleryView.post``), attaches a photo-sourced ``PinVisit`` per new
    image, and enqueues ``process_image_upload`` for each so EXIF/downscale
    post-processing matches every other upload path. An asset already
    imported to this pin, or one that would exceed the uploader's storage
    quota, is skipped rather than failing the whole batch.

    Args:
        pin_id: PK of the pin to import onto.
        profile_id: PK of the requesting profile (also the pin owner).
        asset_ids: Immich asset ids selected in the picker dialog.
        visit_id_by_asset: When importing on behalf of an accepted
            ``PinSuggestion`` (see ``services.pin_suggestions.accept_pin_suggestion``),
            maps an asset id to the specific ``PinVisit`` (already created for
            that suggestion's dates) it should attach to instead of getting a
            fresh one of its own. Omitted assets, and every asset when this is
            ``None`` (the manual "Import from Immich" picker path), fall back
            to creating their own visit via ``log_visit_on_pin``, unchanged
            from before this parameter existed.

    Returns:
        Counts of imported/skipped/failed assets, surfaced to the polling UI.
    """
    import io

    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.visits.model import PinVisit
    from urbanlens.dashboard.services.apis.immich import ImmichGateway
    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.services.gateway import GatewayRequestError
    from urbanlens.dashboard.services.images import compute_checksum
    from urbanlens.dashboard.services.memories.photos import log_visit_on_pin
    from urbanlens.dashboard.services.storage import quota_error_for_upload

    counts = {"imported": 0, "skipped": 0, "failed": 0}
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = ImmichAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Immich connection no longer exists.")
        return counts

    gateway = ImmichGateway(account=account)
    total = len(asset_ids)
    for index, asset_id in enumerate(asset_ids):
        update_task_progress(self, current=index, total=total, message=f"Importing photo {index + 1} of {total}...")
        try:
            content, filename, _content_type = gateway.get_asset_original(asset_id)
        except GatewayRequestError:
            logger.warning("import_immich_photos: failed to download asset %s for pin %s", asset_id, pin_id, exc_info=True)
            counts["failed"] += 1
            continue

        checksum = compute_checksum(io.BytesIO(content))
        if Image.objects.filter(pin=pin, profile=profile, checksum=checksum).exists():
            counts["skipped"] += 1
            continue
        if quota_error_for_upload(profile, len(content)):
            counts["failed"] += 1
            continue

        target_visit_id = (visit_id_by_asset or {}).get(asset_id)
        target_visit = PinVisit.objects.filter(pk=target_visit_id, pin=pin).first() if target_visit_id else None

        image = Image.objects.create(
            image=ContentFile(content, name=filename),
            pin=pin,
            location=pin.location,
            profile=profile,
            checksum=checksum,
            file_size=len(content),
            source_url=account.asset_web_url(asset_id),
            visit=target_visit,
        )
        if target_visit is None:
            log_visit_on_pin(profile, image, pin)
        safely_enqueue_task(process_image_upload, image.pk)
        counts["imported"] += 1

    summary = f"Imported {counts['imported']}"
    if counts["skipped"]:
        summary += f", skipped {counts['skipped']} duplicate(s)"
    if counts["failed"]:
        summary += f", {counts['failed']} failed"
    update_task_progress(self, current=total, total=total, message=summary + ".")
    return counts


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_immich_library_locations(self, profile_id: int) -> dict[str, int]:
    """Sweep a user's entire Immich library for places they've been.

    Unlike ``import_immich_photos``, this never downloads any photo - it pages
    through the lightweight ``/search/metadata`` listing (GPS + capture date
    + city, already present in the response) and feeds every geotagged asset
    through ``services.pin_suggestions.ingest_location_hits``, which matches
    each coordinate against the profile's existing pins and clusters whatever
    doesn't match into new-pin suggestions. Nothing is created automatically -
    this only produces/updates ``PinSuggestion`` rows for the user to review
    and accept or reject. Only triggered by an explicit "Scan your library"
    action (see ``controllers.immich.ImmichLibraryScanStartView``), never on
    connect.

    Args:
        profile_id: PK of the requesting profile (also the Immich account owner).

    Returns:
        Summary counts (matched/new-pin suggestions touched, assets scanned).
    """
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestionOrigin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.immich import ImmichGateway
    from urbanlens.dashboard.services.gateway import GatewayRequestError
    from urbanlens.dashboard.services.pin_suggestions import LocationHit, ingest_location_hits
    from urbanlens.dashboard.services.visits import visit_logging_allowed

    empty = {"scanned": 0, "matched_suggestions": 0, "new_pin_suggestions": 0}
    profile = Profile.objects.filter(pk=profile_id).first()
    account = ImmichAccount.objects.get_for_profile(profile) if profile is not None else None
    if profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Scan failed: profile or Immich connection no longer exists.")
        return empty
    if not visit_logging_allowed(profile):
        update_task_progress(self, current=0, total=1, message="Scan skipped: visit-history tracking is turned off.")
        return empty

    gateway = ImmichGateway(account=account)
    try:
        library_total = gateway.library_asset_count()
    except GatewayRequestError:
        library_total = 0

    hits: list[LocationHit] = []
    scanned = 0
    try:
        for page, _page_total in gateway.iter_library_assets():
            for asset in page:
                scanned += 1
                if asset.lat is None or asset.lon is None or asset.taken_at is None:
                    continue
                hits.append(LocationHit(latitude=asset.lat, longitude=asset.lon, taken_at=asset.taken_at, label=asset.city, asset_id=asset.id))
            # library_total is the true library-wide count (see library_asset_count) -
            # unlike the deprecated per-page "total" iter_library_assets also yields,
            # which mirrors the current page size and would make this message read
            # "Scanned 194000 of 1000" once scanned outgrows a single page.
            if library_total:
                update_task_progress(self, current=scanned, total=max(library_total, scanned, 1), message=f"Scanned {scanned} of {library_total} photo(s)...")
            else:
                update_task_progress(self, current=scanned, total=max(scanned, 1), message=f"Scanned {scanned} photo(s) so far...")
    except GatewayRequestError as exc:
        update_task_progress(self, current=scanned, total=max(scanned, 1), message=f"Scan failed: {exc}")
        return {**empty, "scanned": scanned}

    update_task_progress(self, current=scanned, total=max(scanned, 1), message="Matching against your pins...")
    summary = ingest_location_hits(profile, hits, origin=PinSuggestionOrigin.IMMICH)

    result = {"scanned": scanned, "matched_suggestions": summary.matched_suggestions, "new_pin_suggestions": summary.new_pin_suggestions}
    total_suggestions = summary.matched_suggestions + summary.new_pin_suggestions
    if total_suggestions:
        NotificationLog.objects.create(
            profile=profile,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.INFO,
            title="Found new locations from your Immich library",
            message=(f"Your Immich library scan found {summary.new_pin_suggestions} possible new pin(s) and {summary.matched_suggestions} visit(s) to pins you already have. Review them in Memories."),
        )
    update_task_progress(self, current=scanned, total=max(scanned, 1), message=f"Scan complete - found {total_suggestions} suggestion(s).")
    return result


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def import_flickr_photos(self, pin_id: int, profile_id: int, photo_ids: list[str]) -> dict[str, int]:
    """Download selected Flickr photos and import them onto a pin.

    Same five-step pipeline as ``import_immich_photos`` (checksum dedupe,
    storage-quota check, ``Image`` creation, ``log_visit_on_pin``,
    ``process_image_upload`` enqueue) - only the download source differs.

    Args:
        pin_id: PK of the pin to import onto.
        profile_id: PK of the requesting profile (also the pin owner).
        photo_ids: Flickr photo ids selected in the picker dialog.

    Returns:
        Counts of imported/skipped/failed photos, surfaced to the polling UI.
    """
    import io

    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.flickr.model import FlickrAccount
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway
    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.services.gateway import GatewayRequestError
    from urbanlens.dashboard.services.images import compute_checksum
    from urbanlens.dashboard.services.memories.photos import log_visit_on_pin
    from urbanlens.dashboard.services.storage import quota_error_for_upload

    counts = {"imported": 0, "skipped": 0, "failed": 0}
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = FlickrAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Flickr connection no longer exists.")
        return counts

    gateway = FlickrGateway(account=account)
    total = len(photo_ids)
    for index, photo_id in enumerate(photo_ids):
        update_task_progress(self, current=index, total=total, message=f"Importing photo {index + 1} of {total}...")
        try:
            content, filename, _content_type = gateway.get_original(photo_id)
        except GatewayRequestError:
            logger.warning("import_flickr_photos: failed to download photo %s for pin %s", photo_id, pin_id, exc_info=True)
            counts["failed"] += 1
            continue

        checksum = compute_checksum(io.BytesIO(content))
        if Image.objects.filter(pin=pin, profile=profile, checksum=checksum).exists():
            counts["skipped"] += 1
            continue
        if quota_error_for_upload(profile, len(content)):
            counts["failed"] += 1
            continue

        image = Image.objects.create(
            image=ContentFile(content, name=filename),
            pin=pin,
            location=pin.location,
            profile=profile,
            checksum=checksum,
            file_size=len(content),
            source_url=account.photo_web_url(photo_id),
        )
        log_visit_on_pin(profile, image, pin)
        safely_enqueue_task(process_image_upload, image.pk)
        counts["imported"] += 1

    summary = f"Imported {counts['imported']}"
    if counts["skipped"]:
        summary += f", skipped {counts['skipped']} duplicate(s)"
    if counts["failed"]:
        summary += f", {counts['failed']} failed"
    update_task_progress(self, current=total, total=total, message=summary + ".")
    return counts


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def import_google_photos(self, pin_id: int, profile_id: int, session_id: str, media_item_ids: list[str]) -> dict[str, int]:
    """Download selected Google Photos picker items and import them onto a pin.

    Same five-step pipeline as ``import_immich_photos``/``import_flickr_photos``
    (checksum dedupe, storage-quota check, ``Image`` creation,
    ``log_visit_on_pin``, ``process_image_upload`` enqueue). Each item's
    download URL is resolved from the session-items cache the picker view
    populated when it listed the session (falls back to re-listing the
    session directly if that cache entry expired before the import ran).

    Args:
        pin_id: PK of the pin to import onto.
        profile_id: PK of the requesting profile (also the pin owner).
        session_id: The picker session the items were selected in.
        media_item_ids: Picker API media item ids selected in the picker grid.

    Returns:
        Counts of imported/skipped/failed items, surfaced to the polling UI.
    """
    import io

    from django.core.cache import cache
    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.photos.google import GooglePhotosGateway, media_item_web_url, session_items_cache_key
    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.services.gateway import GatewayRequestError
    from urbanlens.dashboard.services.images import compute_checksum
    from urbanlens.dashboard.services.memories.photos import log_visit_on_pin
    from urbanlens.dashboard.services.storage import quota_error_for_upload

    counts = {"imported": 0, "skipped": 0, "failed": 0}
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = GooglePhotosAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Google Photos connection no longer exists.")
        return counts

    gateway = GooglePhotosGateway(account=account)
    items = cache.get(session_items_cache_key(session_id)) or {}
    missing_ids = [item_id for item_id in media_item_ids if item_id not in items]
    if missing_ids:
        try:
            for item in gateway.list_session_media_items(session_id):
                items[item.id] = {"base_url": item.base_url, "mime_type": item.mime_type, "filename": item.filename}
        except GatewayRequestError:
            logger.warning("import_google_photos: could not re-list session %s to resolve %d missing item(s)", session_id, len(missing_ids), exc_info=True)

    total = len(media_item_ids)
    for index, item_id in enumerate(media_item_ids):
        update_task_progress(self, current=index, total=total, message=f"Importing photo {index + 1} of {total}...")
        cached_item = items.get(item_id)
        if cached_item is None:
            counts["failed"] += 1
            continue
        try:
            content = gateway.download_media_item(cached_item["base_url"], original=True)
        except GatewayRequestError:
            logger.warning("import_google_photos: failed to download item %s for pin %s", item_id, pin_id, exc_info=True)
            counts["failed"] += 1
            continue

        checksum = compute_checksum(io.BytesIO(content))
        if Image.objects.filter(pin=pin, profile=profile, checksum=checksum).exists():
            counts["skipped"] += 1
            continue
        if quota_error_for_upload(profile, len(content)):
            counts["failed"] += 1
            continue

        image = Image.objects.create(
            image=ContentFile(content, name=cached_item.get("filename") or f"{item_id}.jpg"),
            pin=pin,
            location=pin.location,
            profile=profile,
            checksum=checksum,
            file_size=len(content),
            source_url=media_item_web_url(item_id),
        )
        log_visit_on_pin(profile, image, pin)
        safely_enqueue_task(process_image_upload, image.pk)
        counts["imported"] += 1

    summary = f"Imported {counts['imported']}"
    if counts["skipped"]:
        summary += f", skipped {counts['skipped']} duplicate(s)"
    if counts["failed"]:
        summary += f", {counts['failed']} failed"
    update_task_progress(self, current=total, total=total, message=summary + ".")
    return counts


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


# No autoretry, deliberately: the beat scheduler re-fires this every hour
# anyway, and a retry racing the next scheduled run would double-spend the
# API budget the cycle just computed. The time limits keep a slow cycle (many
# sources with long stagger pauses) from ever overlapping the next hourly
# firing; SoftTimeLimitExceeded propagates out of run_enrichment_cycle so the
# task winds down cleanly mid-batch.
@shared_task(bind=True, soft_time_limit=3000, time_limit=3300)
def run_scheduled_enrichment(self) -> dict:
    """Run one background-enrichment cycle when site settings allow it.

    Fired hourly by Celery beat. ``services.enrichment.run_enrichment_cycle``
    checks the admin's enabled toggle and UTC run window, computes how much of
    each API's rate limit is safely spendable (keeping the configured buffer
    in reserve), and enriches the highest-impact Locations still missing
    official names, aliases, addresses, or boundaries.

    Returns:
        The cycle summary dict (also cached for the site-admin page), or a
        skip marker when another run holds the single-flight lock.
    """
    from celery.exceptions import SoftTimeLimitExceeded
    from django.core.cache import cache

    from urbanlens.dashboard.services.enrichment import RUN_LOCK_CACHE_KEY, run_enrichment_cycle

    if not cache.add(RUN_LOCK_CACHE_KEY, 1, 3300):
        logger.info("run_scheduled_enrichment: another cycle is still running; skipping")
        return {"skipped": "already_running"}
    try:
        update_task_progress(self, current=0, total=1, message="Enriching locations...")
        summary = run_enrichment_cycle()
        update_task_progress(self, current=1, total=1, message="Enrichment cycle complete")
        return summary
    except SoftTimeLimitExceeded:
        logger.warning("run_scheduled_enrichment: cycle wound down at the soft time limit")
        return {"skipped": "timed_out"}
    finally:
        cache.delete(RUN_LOCK_CACHE_KEY)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def refresh_pin_web_search(self, pin_id: int) -> int:
    """Pre-warm the shared web-search cache for a pin's Location."""
    from urllib.parse import urlparse

    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.search import format_search_date, search_web

    pin = Pin.objects.filter(pk=pin_id).select_related("location").first()
    query = pin.get_unique_search_name(quote_name=True, quote_locality=True) if pin and pin.location else None
    if not query:
        return 0
    update_task_progress(self, current=0, total=1, message="Refreshing web search...")
    results = search_web(query)
    for result in results:
        try:
            result["domain"] = urlparse(result.get("link", "")).netloc.removeprefix("www.")
        except (ValueError, AttributeError):
            result["domain"] = ""
        result["date_display"] = format_search_date(result.get("date"))
    LocationCache.set(pin.location, "web_search", {"results": results}, query_key=query)
    update_task_progress(self, current=1, total=1, message="Web search refreshed")
    return len(results)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_due_checkin_reminders() -> int:
    """Send the check-in-due reminder for every safety check-in whose time has arrived."""
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.safety import send_checkin_reminder

    count = 0
    for checkin in SafetyCheckin.objects.due_for_reminder():
        send_checkin_reminder(checkin)
        count += 1
    if count:
        logger.info("Sent %s safety check-in reminder(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_final_checkin_warnings() -> int:
    """Send a final "check in now" warning for every safety check-in about to escalate."""
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.safety import send_final_warning

    count = 0
    for checkin in SafetyCheckin.objects.due_for_final_warning():
        send_final_warning(checkin)
        count += 1
    if count:
        logger.info("Sent %s safety check-in final warning(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def escalate_overdue_checkins() -> int:
    """Notify emergency contacts for every safety check-in whose grace period has elapsed."""
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.safety import escalate_checkin

    count = 0
    for checkin in SafetyCheckin.objects.overdue():
        escalate_checkin(checkin)
        count += 1
    if count:
        logger.info("Escalated %s overdue safety check-in(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def delete_expired_safety_checkins() -> int:
    """Permanently delete every resolved safety check-in past its owner's auto-delete window."""
    from urbanlens.dashboard.models.safety.model import SafetyCheckin

    due = SafetyCheckin.objects.due_for_auto_delete()
    count = due.count()
    due.delete()
    if count:
        logger.info("Auto-deleted %s expired safety check-in(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def prune_expired_undo_actions() -> int:
    """Delete UndoAction rows past their retention window.

    The cached payload each row points at has already expired via its own
    TTL by this point - this just tidies up the DB-side index so the
    settings page's history list doesn't need to filter expired rows forever.
    """
    from urbanlens.dashboard.models.undo import UndoAction

    expired = UndoAction.objects.expired()
    count = expired.count()
    expired.delete()
    if count:
        logger.info("Pruned %s expired undo action(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def detect_dm_address_mentions(message_id: int) -> int:
    """Detect street addresses in a direct message's text and record their shares.

    The forward-geocoding half of DM location detection (see
    ``services.dm_location_detection``) - coordinates are detected inline at
    send time, but addresses need a geocoding API call, which never belongs
    in the request path.

    Args:
        message_id: PK of the just-sent message to scan.

    Returns:
        Number of new location mentions recorded.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.services.dm_location_detection import detect_address_mentions

    message = DirectMessage.objects.filter(pk=message_id).select_related("sender", "recipient").first()
    if message is None:
        return 0
    return len(detect_address_mentions(message))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def hard_delete_expired_direct_messages() -> int:
    """Permanently delete every direct message past its sender's disappearing-message window.

    Unlike delete_message_for_everyone (a tombstone - the row and its content
    stay in the DB, just hidden from both parties' rendered view),
    DirectMessage.is_expired_for_recipient only ever gated *display*: the row
    and its body/ciphertext sat in the DB untouched forever. This sweep is
    what actually removes it. Image.direct_message is SET_NULL (not CASCADE),
    so attached images are explicitly deleted here too - otherwise they'd
    survive as orphaned, still-unencrypted files after the message is gone.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.images.model import Image

    due_ids = list(DirectMessage.objects.due_for_hard_delete().values_list("id", flat=True))
    if not due_ids:
        return 0

    for image in Image.objects.filter(direct_message_id__in=due_ids):
        if image.image:
            try:
                image.image.delete(save=False)
            except OSError:
                logger.exception("Failed to delete image file %s for expiring direct message %s", image.pk, image.direct_message_id)
    Image.objects.filter(direct_message_id__in=due_ids).delete()

    count = len(due_ids)
    DirectMessage.objects.filter(id__in=due_ids).delete()
    logger.info("Hard-deleted %s expired direct message(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_account_deletion_reminders() -> int:
    """Send the "1 day left" reminder for every account approaching its hard delete."""
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.account_deletion import send_deletion_reminder

    count = 0
    for profile in Profile.objects.due_for_deletion_reminder():
        send_deletion_reminder(profile)
        count += 1
    if count:
        logger.info("Sent %s account deletion reminder(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def hard_delete_expired_accounts() -> int:
    """Permanently delete every account whose 7-day deletion grace period has elapsed."""
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.account_deletion import hard_delete_profile

    count = 0
    for profile in Profile.objects.due_for_hard_delete():
        hard_delete_profile(profile)
        count += 1
    if count:
        logger.info("Hard-deleted %s expired account(s)", count)
    return count


# No autoretry here, deliberately: run_panel_fetch owns the failure policy
# (suppression markers with their own TTLs), and Celery-level retries would
# race the poll-driven re-scheduling in schedule_panel_fetch. The time limits
# sit under external_data.FLIGHT_TTL_SECONDS so a hard-killed task's
# single-flight marker expires right after the task does.
@shared_task(soft_time_limit=110, time_limit=130)
def fetch_panel_source(source_key: str, pin_id: int) -> None:
    """Fetch one external-data panel's upstream data in the background.

    Scheduled by ``external_data.schedule_panel_fetch`` when a pin detail page
    finds a panel's store empty; the page polls until this task persists the
    result (LocationCache row, Boundary geometry column, or warmed slide caches).

    Args:
        source_key: An ``external_data.panel_sources()`` key.
        pin_id: PK of the pin whose panel data should be fetched.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import run_panel_fetch

    pin = Pin.objects.select_related("location").filter(pk=pin_id).first()
    if pin is None:
        logger.info("fetch_panel_source: pin %s no longer exists", pin_id)
        return
    run_panel_fetch(source_key, pin)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_direct_message_email_if_unread(message_id: int) -> None:
    """Send the delayed "new message" email, unless it's since been read or already sent.

    Scheduled by ``services.direct_messages._schedule_message_email`` with a
    countdown, giving a logged-in recipient a chance to read the message
    organically first. No-ops if the message was read in the meantime, or if
    an earlier message in the same unread streak already triggered this email
    (``services.direct_messages.send_message_email_now`` sets that marker).

    Args:
        message_id: PK of the message to check and possibly email about.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.services.direct_messages import is_email_debounced, send_message_email_now

    try:
        message = DirectMessage.objects.select_related("sender", "recipient__user").get(pk=message_id)
    except DirectMessage.DoesNotExist:
        return
    if message.read_at is not None:
        return
    if is_email_debounced(message.sender_id, message.recipient_id):
        return
    send_message_email_now(message)


@shared_task
def run_link_extraction(extraction_id: int) -> None:
    """Execute one queued AI link-extraction run (fetch, AI call, apply, notify).

    No Celery autoretry: the run itself records every failure mode on the
    LinkExtraction row (and notifies the user either way), and each attempt
    consumes a fetch plus AI tokens - retrying automatically would silently
    multiply cost for a user-triggered, user-visible action they can simply
    click again.

    Args:
        extraction_id: PK of the pending LinkExtraction row.
    """
    from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
    from urbanlens.dashboard.services.ai.link_extraction import run_extraction

    extraction = LinkExtraction.objects.filter(pk=extraction_id).select_related("pin", "pin__location", "profile").first()
    if extraction is None:
        logger.info("run_link_extraction: extraction %s no longer exists", extraction_id)
        return
    run_extraction(extraction)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def upgrade_placeholder_pin_names(batch_size: int = 1000) -> int:
    """Clear a pin's stored placeholder name once its location has a meaningful one to fall back to.

    ``Pin.name`` is meant to be None ("show the location's canonical name")
    unless a user actually typed something - but some pins from earlier,
    less careful ingestion pipelines have a literal placeholder string
    (coordinates, "Dropped Pin", "Unnamed Location", ...) stored directly on
    ``name`` with ``name_is_user_provided=False``. Those pins are stuck
    showing that placeholder forever: ``Pin.effective_name`` only falls back
    to the location's name when ``Pin.name`` is falsy, and nothing else ever
    revisits an already-set name. This sweep finds exactly that case and
    clears ``name`` back to None wherever the location now resolves to a
    meaningful name (e.g. because background enrichment / a later pin at the
    same coordinates has since resolved ``Location.official_name`` or a wiki
    name) - once cleared, ``effective_name`` picks up the better name
    immediately and stays current automatically as the location's name
    improves further, with no further sweeps needed for that pin.

    TODO: This exists only to backfill legacy data from earlier ingestion
    versions that didn't leave ``Pin.name`` as None for an unnamed pin. Once
    ingestion is guaranteed to never store a placeholder name this way, this
    task (and the gap it patches) should be removed - new pins never need it.

    Args:
        batch_size: Maximum number of pins to upgrade in one run, so a single
            invocation can't run unboundedly long; any remainder is picked up
            by the next scheduled run.

    Returns:
        Number of pins whose name was cleared.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    upgraded = 0
    for pin in Pin.objects.with_placeholder_names().iterator(chunk_size=200):
        if is_meaningful_name(pin.name):
            continue
        if not is_meaningful_name(pin.location.display_name):
            continue
        pin.name = None
        pin.save(update_fields=["name", "updated"])
        upgraded += 1
        if upgraded >= batch_size:
            break
    if upgraded:
        logger.info("upgrade_placeholder_pin_names: cleared %s placeholder pin name(s)", upgraded)
    return upgraded
