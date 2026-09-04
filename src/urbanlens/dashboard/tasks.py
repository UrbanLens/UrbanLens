"""Celery tasks for the dashboard application.

Tasks that hand untrusted uploaded bytes to a parser declare
``queue=SANDBOX_QUEUE`` (or ``SANDBOX_BATCH_QUEUE`` when the parse is a
minutes-long batch job), which routes them to an isolated ``media-worker``
container rather than the general-purpose worker. The queue is declared on the
task instead of at each ``apply_async`` site on purpose - see
:mod:`urbanlens.dashboard.services.sandbox.queues` for why, and
:mod:`urbanlens.dashboard.services.sandbox.guard` for what the isolation buys.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.utils import timezone

from urbanlens.dashboard.services.ai.tasks import (
    run_assistant_turn_task,
)
from urbanlens.dashboard.services.core.celery import update_task_progress
from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
from urbanlens.dashboard.services.sandbox import sandbox_queue

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: Resolved once, at import, so it lands in each decorated task's own exec
#: options. Falls back to the default queue where no sandbox worker is deployed
#: (``UL_SANDBOX_ENABLED=false``), so those installs keep processing uploads
#: rather than filling a queue nothing drains.
SANDBOX_QUEUE = sandbox_queue()
#: For untrusted parses that run for minutes, not milliseconds - same isolation,
#: separate worker, so they cannot occupy the interactive pool.
SANDBOX_BATCH_QUEUE = sandbox_queue(batch=True)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def ensure_wiki_for_location(location_id: int) -> int | None:
    """Auto-create the Wiki for a Location, so enrichment can get a head start.

    Queued by the ``Pin`` post_save signal (``models.pin.signals``) whenever a
    pin gets a shared Location, for any community-enabled profile - covering
    every pin-creation path (manual add, CSV/Google Maps import, Flickr,
    Immich, GPX) with one hook. The row itself is a cheap DB-only write; no
    external API is touched here or by the signal that queued this - that
    only happens below, once, when the draft is first created.

    The page is published from the moment it exists - there is no draft state
    and nothing for a user to "create". It starts empty and fills in as
    enrichment lands, which is what a place nobody has written up looks like
    anyway.

    Args:
        location_id: PK of the Location that just gained a pin.

    Returns:
        PK of the Wiki (new or pre-existing), or None if the Location no
        longer exists.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("ensure_wiki_for_location: location %s no longer exists", location_id)
        return None

    wiki, created = Wiki.objects.get_or_create_for_location(location)
    if created:
        safely_enqueue_task(enrich_wiki_location, wiki.pk)
        # Covers a Wikipedia article matched and cached for this location
        # *before* there was a wiki to seed. The other direction - a match
        # caching after the wiki exists - is handled by models.cache.signals.
        # This used to hang off the "Create wiki" click, which was the moment
        # the page appeared; that moment is here now.
        from urbanlens.dashboard.services.wiki.wiki_seed import seed_wiki_article_from_wikipedia

        seed_wiki_article_from_wikipedia(location)
    return wiki.pk


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def enrich_wiki_location(self, wiki_id: int) -> bool:
    """Enrich a Wiki's Location with external data.

    Runs right after ``ensure_wiki_for_location`` creates the page: links the
    Location to its Google Place, resolves a canonical
    name when the wiki is still unnamed, and generates the location's default
    property/building boundaries. This is the only place these APIs are hit
    for a wiki - pin creation and bulk imports never call them synchronously.

    Args:
        wiki_id: PK of the Wiki to enrich.

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
def mirror_buildings_to_wiki(pin_id: int, selection_keys: list[str]) -> int:
    """Mirror imported buildings onto the community wiki, off the request.

    The pin side of a building import has already succeeded by the time this
    runs, so nothing here may fail it: a wiki-side problem must not surface as
    a 500 for work that was already done (see docs/PROBLEMS.md, 2026-08-18).

    Takes selection keys rather than the building records themselves so the
    task body stays small and re-resolves against the current cache - a stale
    key simply finds nothing.

    Args:
        pin_id: The parent pin whose buildings were imported.
        selection_keys: ``building_selection_key`` values for the imported
            buildings.

    Returns:
        How many child wikis were created.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.locations import site_scope
    from urbanlens.dashboard.services.pins import pin_restructure

    pin = Pin.objects.filter(pk=pin_id).select_related("location", "profile").first()
    if pin is None:
        return 0
    buildings = pin_restructure.select_buildings(site_scope.parcel_buildings(pin.location) or [], selection_keys)
    if not buildings:
        return 0
    return pin_restructure.mirror_buildings_to_wiki(pin, buildings, pin.profile)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def auto_nest_building_pins(pin_id: int) -> int:
    """Build a new pin's default child-pin structure from cached building data.

    Enqueued at pin creation when the location's building list is already
    cached (another user pinned it first) - creating up to a campus worth of
    child pins is not request-time work. When nothing is cached yet, the
    fetch/enrichment paths run the same sweep once the list arrives instead.

    Args:
        pin_id: The freshly-created root pin.

    Returns:
        How many child pins were created, or 0 when the pin is gone or not
        eligible.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.auto_nest import auto_nest_pin

    pin = Pin.objects.filter(pk=pin_id).select_related("location", "profile").first()
    if pin is None:
        return 0
    return auto_nest_pin(pin)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def generate_boundaries_for_location(location_id: int) -> bool:
    """Generate (or, if stale, refresh) the default property/building boundaries for a Location.

    Scheduled single-flight by ``schedule_location_boundary_generation`` (wiki
    page, and the Private Pin page's stale-refresh path) - the pin detail
    page's first-ever generation uses the "boundary" panel source instead,
    which calls the same ``generate_location_boundaries`` function.

    Args:
        location_id: PK of the Location.

    Returns:
        True when the location existed and generation ran (or was already
        fresh).
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.boundaries import generate_location_boundaries, generation_lock_key, generation_status

    try:
        location = Location.objects.filter(pk=location_id).first()
        if location is None:
            logger.info("generate_boundaries_for_location: location %s no longer exists", location_id)
            return False
        ran, stale = generation_status(location)
        if not ran or stale:
            generate_location_boundaries(location)
        return True
    finally:
        cache.delete(generation_lock_key(location_id))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def classify_detail_marker(kind: str, marker_id: int) -> bool:
    """Decide whether a newly placed child pin/wiki stands on a building.

    Queued whenever a sub-marker is created or moved without the user
    choosing a type themselves (see ``controllers.detail_pins``). Generating
    the marker's own boundaries first is the whole point: the provider chain
    only fills a location's ``BUILDING`` boundary when some provider has a
    footprint polygon containing that exact point, which is precisely the
    question being asked.

    Runs on the default (prefork) queue rather than ``panel_fetch``: boundary
    generation does real CPU-bound geometry work, and a campus import queues
    one of these per building. See ``PanelSource.queue`` for the same reasoning.

    Args:
        kind: ``"pin"`` or ``"wiki"``.
        marker_id: PK of the Pin or Wiki to classify.

    Returns:
        True when the marker was reclassified as a building.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, generate_location_boundaries
    from urbanlens.dashboard.services.locations.site_scope import classify_building_pin_type

    model = Pin if kind == "pin" else Wiki
    marker = model.objects.select_related("location").filter(pk=marker_id).first()
    if marker is None:
        logger.info("classify_detail_marker: %s %s no longer exists", kind, marker_id)
        return False
    if marker.pin_type_is_user_provided:
        return False

    location = marker.location
    if location is not None and not boundary_generation_ran(location):
        generate_location_boundaries(location)

    return classify_building_pin_type(marker)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def warm_saved_filter_cache(profile_id: int) -> int:
    """Precompute and cache a profile's saved-filter matching-pin uuid lists.

    Queued right after login (see ``models.profile.signals``) so the bottom-right
    map toolbar's first filter toggle of the session hits a warm
    ``services.search.saved_filter_cache`` entry instead of a cold query.

    Args:
        profile_id: PK of the ``Profile`` to warm - never a bare user-supplied
            uuid, so this can't be used to warm (or probe) another user's data.

    Returns:
        Number of saved filters warmed, or 0 if the profile no longer exists.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.search.saved_filter_cache import warm_all_for_profile

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
    from urbanlens.dashboard.services.trips.calendar_sync import push_auto_synced_trip_changes

    trip = Trip.objects.filter(pk=trip_id).first()
    if trip is None:
        logger.info("push_trip_to_calendar: trip %s no longer exists", trip_id)
        return 0
    return push_auto_synced_trip_changes(trip)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def run_user_data_export(self, user_id: int, export_types: list[str], export_dir: str, base_url: str, job_id: str | None = None, email_to_user: bool = False) -> bool:
    """Build a user's data export archive outside the web request."""
    from urbanlens.dashboard.services.import_export.export import run_export

    logger.info("Starting data export for user %s", user_id)
    update_task_progress(self, current=0, total=1, message="Preparing export...")
    success = run_export(user_id, export_types, export_dir, base_url, job_id=job_id, email_to_user=email_to_user)
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
    from urbanlens.dashboard.services.import_export.export import ExportJobStatus, cleanup_export_artifacts

    cleanup_export_artifacts(export_dir, ExportJobStatus(job_id) if job_id else None)
    logger.info("Cleaned up export artifacts for job %s", job_id or export_dir)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=SANDBOX_BATCH_QUEUE)
def run_user_data_import(self, user_id: int, zip_path: str, job_id: str) -> bool:
    """Parse a UrbanLens export ZIP and import data for the user."""
    from urbanlens.dashboard.services.import_export.import_data import run_import

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
    from urbanlens.dashboard.services.import_export.import_data import ImportJobStatus, cleanup_import_artifacts

    cleanup_import_artifacts(import_dir_path, ImportJobStatus(job_id) if job_id else None)
    logger.info("Cleaned up import artifacts for job %s", job_id or import_dir_path)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def cleanup_vestigial_assets_task() -> dict[str, int]:
    """Sweep stale import/export artifacts missed by per-job cleanup tasks."""
    from urbanlens.dashboard.services.import_export.vestigial_assets import cleanup_vestigial_assets

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
    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        logger.info("rebuild_map_pin_cache: profile %s no longer exists", profile_id)
        return 0
    query = Pin.objects.filter(profile=profile).root_pins().select_related("location")
    cache = MapPinCache(profile)
    cache.rebuild(query)
    update_task_progress(self, current=1, total=1, message="Map cache ready")
    return query.count()


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def suggest_wiki_category(self, wiki_id: int) -> list[str]:
    """Suggest and attach labels for a community Wiki outside model signals."""
    from urbanlens.dashboard.models.wiki import Wiki
    from urbanlens.dashboard.services.labels.auto_tag import AutoTagService

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
    from urbanlens.dashboard.services.labels.auto_tag import AutoTagService

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
    from urbanlens.dashboard.services.apis.locations.wayback_machine import WaybackMachineGateway, is_own_site_url

    model = {"PinLink": PinLink, "WikiLink": WikiLink}.get(link_model)
    if model is None:
        logger.warning("archive_link_to_wayback: unknown link_model %r", link_model)
        return False

    link = model.objects.filter(pk=link_id).first()
    if link is None or link.wayback_url:
        return False

    if is_own_site_url(link.url):
        # Most of our own pages require being logged in - archiving them would
        # only ever save an unreadable login wall, not the actual content.
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
    Private Pin page can skip the Places Details API call.

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

    # NPS: caches the nearest park unit to the location, if any is within
    # REData's search radius (see plugins.builtin.nps for why this is a
    # proximity search rather than the boundary-containment lookup this used
    # to be).
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

    if redata_configured() and LocationCache.get_fresh(location, "nps") is None:
        try:
            from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

            park = RedataNationalParksGateway().find_nearest_park(lat, lng)
            LocationCache.set(location, "nps", park or {}, query_key=f"{lat:.5f},{lng:.5f}")
            logger.info("prefetch_location_external_data: cached NPS for location %s", location_id)
        except Exception:
            logger.exception("prefetch_location_external_data: NPS lookup failed for location %s", location_id)

    # Google Places - migrate from Django request cache into LocationCache so the
    # Private Pin page can display it without a fresh API call.
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


def _process_photo_upload(image: Image, image_id: int, strip_location: bool, max_dimension_override: int | None = None) -> _UploadProcessResult | None:
    """Photo-specific metadata extraction and downscaling.

    Args:
        image: The row to process.
        image_id: Its pk, for log lines that must survive a deleted row.
        strip_location: Whether to discard the coordinates rather than record them.
        max_dimension_override: Longest-edge cap for a row with no profile to
            derive a plan policy from - see :func:`process_image_upload`.

    Returns:
        The fields to write back, or None on unrecoverable read failure (the
        caller treats that as a failed task run).
    """
    from decimal import Decimal

    from PIL.Image import DecompressionBombError as PILDecompressionBombError

    from urbanlens.dashboard.services.media.images import (
        compute_checksum,
        downscale_stored_image,
        extract_aperture,
        extract_author,
        extract_camera_info,
        extract_caption_from_metadata,
        extract_copyright_notice,
        extract_exif_data,
        extract_focal_length,
        extract_gps_altitude,
        extract_gps_coords,
        extract_gps_direction,
        extract_gps_orientation,
        extract_lens_model,
        extract_shutter_speed,
        extract_source_url,
        extract_taken_at,
        is_camera_generated_filename,
        write_image_analysis_thumbnail,
        write_image_marker_thumbnail,
        write_image_thumbnail,
    )
    from urbanlens.dashboard.services.media.storage import get_downscale_policy

    try:
        with image.image.open("rb") as image_file:
            coords = None if strip_location else extract_gps_coords(image_file)
            # Same GPS-IFD-derived, same privacy opt-out as coords above - the
            # compass bearing is only ever meaningful alongside a location.
            direction = None if strip_location else extract_gps_direction(image_file)
            # exif_altitude/exif_pitch/exif_roll are write-once (never
            # overwritten once set, unlike coords/direction above), so skip the
            # read entirely once a row already carries them.
            altitude = None if strip_location or image.exif_altitude is not None else extract_gps_altitude(image_file)
            orientation = None if strip_location or image.exif_pitch is not None else extract_gps_orientation(image_file)
            taken_at = extract_taken_at(image_file)
            checksum = compute_checksum(image_file) if not image.checksum else None
            exif_data = extract_exif_data(image_file) if image.exif_data is None else None
            author = extract_author(image_file) if not image.author else None
            copyright_notice = extract_copyright_notice(image_file) if not image.copyright else None
            metadata_caption = extract_caption_from_metadata(image_file) if not image.caption else None
            source_url = extract_source_url(image_file) if not image.source_url else None
            camera_make, camera_model = (None, None) if (image.exif_camera_make or image.exif_camera_model) else extract_camera_info(image_file)
            lens_model = extract_lens_model(image_file) if not image.exif_lens_model else None
            shutter_speed = extract_shutter_speed(image_file) if not image.exif_shutter_speed else None
            aperture = extract_aperture(image_file) if image.exif_aperture is None else None
            focal_length = extract_focal_length(image_file) if image.exif_focal_length is None else None
    except (OSError, ValueError) as exc:
        logger.warning("Image metadata extraction failed for image %s: %s", image_id, exc, exc_info=True)
        return None

    # Dropping GPSInfo makes "what did the EXIF say" permanently unanswerable
    # for this photo - the deliberate exception to exif_data being the surviving
    # record of EXIF provenance. That is the point of the opt-out, not an
    # oversight; any future coordinate-provenance work must treat a
    # location-stripped photo as having no EXIF position rather than an unknown one.
    if strip_location and exif_data:
        exif_data.pop("GPSInfo", None)

    # An upload accepted through services.photos reads its metadata in the
    # request and stores the file already stripped, so by the time this task
    # runs there is nothing left in the bytes to find - the row is where the
    # coordinates are. Falling back to them keeps location resolution and the
    # visit suggestion below working for those rows, and is a no-op for a file
    # that still carries its own (an older row, or a format the byte-level
    # stripper leaves to the re-encode).
    if coords is None and not strip_location and image.latitude is not None and image.longitude is not None:
        coords = (float(image.latitude), float(image.longitude))

    update_fields: dict[str, object] = {}
    if direction is not None:
        image.direction = Decimal(str(round(direction, 2)))
        update_fields["direction"] = image.direction
    if not strip_location and image.exif_latitude is None and coords is not None:
        image.exif_latitude = Decimal(str(round(coords[0], 6)))
        image.exif_longitude = Decimal(str(round(coords[1], 6)))
        update_fields["exif_latitude"] = image.exif_latitude
        update_fields["exif_longitude"] = image.exif_longitude
    if altitude is not None:
        image.exif_altitude = Decimal(str(round(altitude, 2)))
        update_fields["exif_altitude"] = image.exif_altitude
    if orientation is not None:
        image.exif_pitch = Decimal(str(round(orientation[0], 2)))
        image.exif_roll = Decimal(str(round(orientation[1], 2)))
        update_fields["exif_pitch"] = image.exif_pitch
        update_fields["exif_roll"] = image.exif_roll
    if camera_make:
        image.exif_camera_make = camera_make
        update_fields["exif_camera_make"] = camera_make
    if camera_model:
        image.exif_camera_model = camera_model
        update_fields["exif_camera_model"] = camera_model
    if lens_model:
        image.exif_lens_model = lens_model
        update_fields["exif_lens_model"] = lens_model
    if shutter_speed:
        image.exif_shutter_speed = shutter_speed
        update_fields["exif_shutter_speed"] = shutter_speed
    if aperture is not None:
        image.exif_aperture = Decimal(str(round(aperture, 1)))
        update_fields["exif_aperture"] = image.exif_aperture
    if focal_length is not None:
        image.exif_focal_length = Decimal(str(round(focal_length, 1)))
        update_fields["exif_focal_length"] = image.exif_focal_length
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

    if image.profile is not None and not (image.author or image.source_url or image.caption or image.copyright) and is_camera_generated_filename(image.original_filename or ""):
        uploader_name = image.profile.full_name or image.profile.username
        if uploader_name:
            image.author = uploader_name
            update_fields["author"] = uploader_name

    new_stored_size: int | None = None
    if image.profile is not None:
        downscale_policy: tuple[int | None, bool] | None = get_downscale_policy(image.profile)
    else:
        # A profile-less row (location enrichment) has no plan to read a policy
        # from; its caller passes the cap instead. Falling back to a default
        # rather than skipping, because this call is also what strips the
        # provider's EXIF - "no cap given" must not silently mean "publish the
        # provider's original, GPS and all". WebP conversion is not optional
        # here either: these are provider photos kept as gallery thumbnails.
        from urbanlens.dashboard.services.photos.photo_enrichment import DEFAULT_ENRICHED_MAX_DIMENSION

        downscale_policy = (max_dimension_override if max_dimension_override is not None else DEFAULT_ENRICHED_MAX_DIMENSION, True)
    if downscale_policy is not None:
        max_dimension, convert_webp = downscale_policy
        # Called unconditionally. It used to be gated on there being a resize, a
        # conversion, a location opt-out or a HEIC to transcode - reasonable while
        # this function existed to resize, but it is also what removes EXIF now,
        # and "no cap, no conversion" is exactly the policy a downscale-exempt
        # subscriber gets. Gating it left their photos carrying the block.
        # downscale_stored_image decides for itself whether anything needs doing,
        # including the HEIC case (`stored_file_needs_transcode`), where the stored
        # bytes are what a plain <img src> gets and most browsers cannot render them.
        try:
            new_size = downscale_stored_image(image, max_dimension, convert_webp)
        except (OSError, ValueError, PILDecompressionBombError) as exc:
            # DecompressionBombError inherits straight from Exception, not from
            # OSError/ValueError like the rest of Pillow's failures (Unidentified-
            # ImageError does), so it escaped this handler and took the whole
            # photo-processing task down with it. Pillow's own 89MP ceiling already
            # prevents the memory exhaustion; what was missing was degrading to the
            # same logged warning every other unprocessable image gets, leaving the
            # upload stored and the rest of the pipeline intact.
            logger.warning("Downscaling failed for image %s: %s", image_id, exc, exc_info=True)
        else:
            if new_size is not None:
                update_fields["image"] = image.image.name
                new_stored_size = new_size

    try:
        if write_image_thumbnail(image):
            update_fields["thumbnail"] = image.thumbnail.name
    except (OSError, ValueError, PILDecompressionBombError) as exc:
        # A miss here is retried by the hourly backfill_image_thumbnails sweep
        logger.warning("Thumbnail generation failed for image %s: %s", image_id, exc, exc_info=True)

    try:
        if write_image_marker_thumbnail(image):
            update_fields["marker_thumbnail"] = image.marker_thumbnail.name
    except (OSError, ValueError, PILDecompressionBombError) as exc:
        # A miss here is retried by the hourly backfill_image_marker_thumbnails sweep
        logger.warning("Marker thumbnail generation failed for image %s: %s", image_id, exc, exc_info=True)

    try:
        if write_image_analysis_thumbnail(image):
            update_fields["analysis_thumbnail"] = image.analysis_thumbnail.name
    except (OSError, ValueError, PILDecompressionBombError) as exc:
        # A miss here is retried by the hourly backfill_image_analysis_thumbnails
        # sweep. Keywording skips a photo that has no analysis copy rather than
        # decoding one itself - see services.photos.photo_keywords.
        logger.warning("Analysis thumbnail generation failed for image %s: %s", image_id, exc, exc_info=True)

    return _UploadProcessResult(update_fields, coords, new_stored_size)


def _process_video_upload(image: Image, strip_location: bool) -> _UploadProcessResult:
    """Video-specific metadata extraction (via ffprobe) and downscaling (via ffmpeg)."""
    from urbanlens.dashboard.services.media.storage import get_video_downscale_policy
    from urbanlens.dashboard.services.media.videos import process_uploaded_video

    max_height = get_video_downscale_policy(image.profile) if image.profile is not None else None
    # The container's own location tags are always removed from the stored file;
    # strip_location decides only whether the coordinates are recorded on the
    # row, where the app's visibility rules govern them.
    metadata, new_size = process_uploaded_video(image, max_height)

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
    from urbanlens.dashboard.services.media.documents import convert_to_pdf, extract_pdf_text

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


def _sync_deduped_siblings(image: Image) -> None:
    """Copy the processed file and its metadata onto this user's other rows of the same bytes.

    Deduplicated copies skip ``process_image_upload`` so they don't rewrite the
    shared file. A copy made while the original was still pending points at the
    raw upload, so ``image`` is synced along with the metadata: without it the
    sibling keeps serving the un-stripped file, GPS block and all, and nothing
    else ever revisits it (the thumbnail backfill only looks at rows that have
    no thumbnail, and this function has just given it one).

    Also clears ``pending_scan`` on every sibling. ``attach_deduped_copy`` gives
    a fresh sibling the same ``pending_scan`` its original had at that moment
    (so it isn't immediately visible in its own, different pin/wiki while the
    shared file is still raw) - but a dedup sibling never runs this task itself,
    so this is the only place anything ever clears it again.

    ``checksum`` is deliberately not synced. It identifies the *uploaded* bytes
    and is what dedup matches on; it is not recomputed after processing.
    """
    from urbanlens.dashboard.models.images.model import Image as ImageModel, QuotaExemption
    from urbanlens.dashboard.services.media.images import file_still_referenced

    if not image.checksum or image.profile_id is None:
        return
    if image.quota_exempt_reason == QuotaExemption.DEDUPLICATED:
        return
    processed_name = image.image.name if image.image else ""
    payload: dict[str, object] = {
        "author": image.author,
        "copyright": image.copyright,
        "taken_at": image.taken_at,
        "latitude": image.latitude,
        "longitude": image.longitude,
        "direction": image.direction,
        "exif_data": image.exif_data,
        "file_size": image.file_size,
        "thumbnail": image.thumbnail.name if image.thumbnail else "",
        "marker_thumbnail": image.marker_thumbnail.name if image.marker_thumbnail else "",
        "pending_scan": image.pending_scan,
    }
    if processed_name:
        payload["image"] = processed_name

    siblings = ImageModel.objects.filter(
        profile_id=image.profile_id,
        checksum=image.checksum,
        quota_exempt_reason=QuotaExemption.DEDUPLICATED,
    ).exclude(pk=image.pk)
    stale_names = {name for name in siblings.values_list("image", flat=True) if name and name != processed_name}
    siblings.update(**payload)

    # Those siblings were the only reason downscale_stored_image kept the raw
    # upload; once they point at the processed file it is unreferenced.
    for name in stale_names:
        if not file_still_referenced("image", name):
            with contextlib.suppress(OSError):
                image.image.storage.delete(name)


def _scan_pending_upload(task, image: Image) -> bool:
    """Run the malware scan a pending upload has not had yet.

    The scan used to block the upload request - a clamd round-trip, 15s socket
    timeout, in the request path, for every photo. It runs here instead, which
    is what makes an upload return immediately; ``Image.pending_scan`` is what
    makes that safe, since nobody but the uploader can see the row until this
    clears it.

    Gated on ``pending_scan`` rather than run unconditionally, so this is a
    no-op for anything already scanned: a legacy row being reprocessed by a
    backfill, a dedup sibling, or an upload from a surface that still scans
    synchronously. That gate is also why a call site missed when the sync scan
    was removed fails *safe* - it scans twice rather than not at all.

    Args:
        task: The bound Celery task, for ``retry``.
        image: The pending row whose stored file to scan.

    Returns:
        True when the file is clean and processing should continue. False when
        the row has been rejected and removed - the caller must stop.

    Raises:
        celery.exceptions.Retry: clamd was unreachable and retries remain.
    """
    from urbanlens.dashboard.services.security.malware_scan import (
        VIRUSTOTAL_ELIGIBLE_SOURCES,
        MalwareScanUnavailableError,
        malware_error_for_fetched_asset,
        malware_error_for_upload,
    )

    try:
        with image.image.open("rb") as stored:
            if image.source in VIRUSTOTAL_ELIGIBLE_SOURCES:
                malware_error = malware_error_for_fetched_asset(stored, checksum=image.checksum)
            else:
                malware_error = malware_error_for_upload(stored)
    except MalwareScanUnavailableError as exc:
        if task.request.retries < task.max_retries:
            # A clamd hiccup must not reject somebody's photo. Same backoff the
            # comment scan uses; the upload stays pending (invisible to anyone
            # else) for as long as this takes.
            raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc
        logger.exception("Malware scan permanently unavailable for image %s after %s retries", image.pk, task.request.retries)
        _reject_image_upload(image, "Our antivirus scanner was unavailable, so this upload could not be checked and was removed. Please try again.")
        return False
    except OSError as exc:
        # The stored file could not be opened at all. This used to fall through
        # to the media-type branch on the theory that it owns the retry-then-
        # reject policy - but only the *photo* branch has one. A document or a
        # video always produces a result (their converters swallow their own
        # failures), so falling through cleared pending_scan and published a
        # file clamd had never seen. Treated as a scan failure instead, which is
        # what it is: not scanned.
        if task.request.retries < task.max_retries:
            raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc
        logger.exception("Could not open image %s to scan it, after %s retries", image.pk, task.request.retries)
        _reject_image_upload(image, "We couldn't read this file to check it, so it was removed. You can try uploading it again.")
        return False

    if malware_error:
        _reject_image_upload(image, malware_error)
        return False
    return True


def _reject_image_upload(image: Image, reason: str) -> None:
    """Notify an uploader their upload was rejected, and remove it.

    Only ever called for a row that is still ``pending_scan`` - see the one
    call site in :func:`process_image_upload` - so nobody but the uploader has
    ever been able to see it; removing it and notifying them is simpler than
    leaving a permanently-broken "processing failed" row behind.

    Also rejects every dedup sibling pointing at the same stored file
    (``attach_deduped_copy`` copies ``pending_scan`` from its original at
    creation, but nothing besides *this* function ever runs on a sibling
    row - unlike :func:`_sync_deduped_siblings`, which only fires on success -
    so leaving them would strand each one hidden forever with no path to
    either clearing or removal). ``delete_stored_file`` still only removes the
    file once nothing references it, so siblings deleted first do not orphan
    the last row still pointing at it.

    Args:
        image: The still-pending ``Image`` - photo, video or document - that was
            condemned by the scan or whose processing failed permanently.
        reason: The user-facing reason, e.g. the scanner's own message.
    """
    from django.urls import NoReverseMatch, reverse

    from urbanlens.dashboard.models.images.model import Image as ImageModel, QuotaExemption
    from urbanlens.dashboard.models.notifications.meta import NotificationType
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.media.images import delete_stored_file
    from urbanlens.dashboard.services.photos.uploads import record_photo_upload_failure

    siblings = list(
        ImageModel.objects.filter(
            profile_id=image.profile_id,
            checksum=image.checksum,
            quota_exempt_reason=QuotaExemption.DEDUPLICATED,
        ).exclude(pk=image.pk)
        if image.checksum and image.profile_id is not None
        else []
    )
    sibling_pks = [sibling.pk for sibling in siblings]

    url = ""
    try:
        if image.pin is not None:
            url = reverse("pin.details", kwargs={"pin_slug": image.pin.slug or str(image.pin.uuid)})
        elif image.wiki is not None and image.wiki.location_id:
            url = reverse("location.wiki", kwargs={"location_slug": image.wiki.location.slug or str(image.wiki.location.uuid)})
        else:
            url = reverse("vault.photos")
    except NoReverseMatch:
        logger.warning("Could not build a photo URL while notifying about a rejected upload (image %s)", image.pk)

    if image.profile is not None:
        NotificationLog.objects.notify(
            profile=image.profile,
            notification_type=NotificationType.PHOTO_UPLOAD_FAILED,
            title="An upload could not be processed",
            message=reason,
            url=url,
        )
        record_photo_upload_failure(image.profile, image.original_filename or "photo", reason, pin=image.pin)

    for sibling in siblings:
        delete_stored_file(sibling, also_deleting=[image.pk, *sibling_pks])
        sibling.delete()
    delete_stored_file(image, also_deleting=sibling_pks)
    image.delete()


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=SANDBOX_QUEUE)
def process_image_upload(self, image_id: int, max_dimension: int | None = None) -> bool:
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

    Args:
        image_id: PK of the row to process.
        max_dimension: Longest-edge cap to downscale to when the row has **no
            profile** - a location-enrichment photo fetched from a provider
            (``photos.photo_enrichment``), which has no subscriber plan to read
            a policy from. Ignored for an ordinary upload, whose uploader's plan
            decides. Without it such a row is stored at whatever size the
            provider returned and keeps the provider's EXIF.

    Returns:
        True when the row was processed. False when it no longer exists, has no
        file, or was rejected (scan or unrecoverable processing failure).
    """
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit
    from urbanlens.dashboard.services.visits.visits import visit_logging_allowed

    update_task_progress(self, current=0, total=1, message="Processing upload metadata...")
    image = Image.objects.filter(pk=image_id).select_related("pin__location", "wiki__location", "profile").first()
    if image is None or not image.image:
        return False

    if image.pending_scan and not _scan_pending_upload(self, image):
        # Infected, or unscannable after every retry. The row and its file are
        # already gone (_reject_image_upload); nothing left to process.
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
        photo_result = _process_photo_upload(image, image_id, strip_location, max_dimension)
        if photo_result is None:
            if not image.pending_scan:
                # Reprocessing an already-cleared row (a backfill sweep, a
                # manual re-enqueue) failed to open a file that was previously
                # fine - unrelated to the raw-upload window pending_scan
                # guards, and there is nothing new to protect here. Same
                # degrade as before pending_scan existed: log (already done
                # inside _process_photo_upload) and leave the row as-is.
                return False
            # A fresh upload's stored file could not be opened at all -
            # _process_photo_upload's own try/except swallows the OSError/
            # ValueError rather than raising, specifically so this task's
            # autoretry_for=(OSError,) never sees it and never retries on its
            # own; explicit retry here scopes that decision to this one
            # failure. Worth retrying: a storage backend hiccup, or the
            # upload not yet fully visible to this worker, both resolve on
            # their own within a few seconds.
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=min(60 * (2**self.request.retries), 900))
            # Retries exhausted. Unlike a legacy row, there is no "already
            # safe, just missing some metadata" fallback available for a
            # pending row - nothing has ever opened this file successfully,
            # so it is either ordinary corruption or exactly the class of
            # deliberately-malformed file (CVE-2023-4863 is the reference
            # case) pending_scan exists to keep unpublished. Clearing
            # pending_scan and serving it anyway would be precisely that
            # leak; the only safe outcome is removal, mirroring a
            # confirmed-infected comment upload (_reject_comment_upload).
            _reject_image_upload(image, "We couldn't process this photo, so it was removed. You can try uploading it again.")
            return False
        result = photo_result

    update_fields, coords = result.update_fields, result.coords

    if image.pending_scan:
        # Set at upload time - see Image.pending_scan. Everything it was hidden
        # for has now happened: the malware scan at the top of this task, plus
        # the metadata read and downscale/transcode below.
        #
        # Set on the Python object too, not just update_fields: _sync_deduped_siblings
        # below reads image.pending_scan (via this same object) to decide whether a
        # dedup sibling created while this row was still pending needs clearing too -
        # matching how image.latitude/longitude are kept in sync just above for the
        # same reason.
        image.pending_scan = False
        update_fields["pending_scan"] = False

    # Unconditional, unlike the exif_data write in _process_photo_upload, which
    # only fills a row that has none. A re-enqueued or retried run therefore
    # replaces a manually placed position with the EXIF one.
    # TODO: guard this once Image records which provenance its coordinates have
    # (see the latitude/longitude comment in models/images/model.py). Left as-is
    # deliberately: changing it without that column would trade one silent
    # overwrite for another.
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
    image.upload_processed_at = timezone.now()
    update_fields["upload_processed_at"] = image.upload_processed_at

    if image.location_id is None:
        location = _resolve_image_location(image, coords)
        if location is not None:
            image.location = location
            update_fields["location"] = location

    if update_fields:
        Image.objects.filter(pk=image_id).update(**update_fields)

    _sync_deduped_siblings(image)

    if not strip_location:
        maybe_suggest_photo_visit(image)

    # Keyword generation runs as its own task so a slow provider (AI vision,
    # classifiers) never delays the metadata/downscale pipeline above; it also
    # deliberately runs after the downscale so providers read the final file.
    # Photo-keyword plugins are built around analyzing a raster image, so this
    # only applies to actual photos - videos/documents are made searchable via
    # their own metadata/ocr_text instead.
    if image.media_type == MediaKind.PHOTO:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task as _enqueue

        # A profile-less row (location enrichment, provider photos) has no
        # uploader who opted into keyword generation, and no gallery of their
        # own for the keywords to be searched from - so it does not get the
        # (plugin-dependent, possibly billed) vision pass. Before enrichment was
        # routed through this task the condition read `profile is None or ...`,
        # which was only ever exercised by rows that had a profile anyway; left
        # as-is it would have started an AI call per hourly Street View tile.
        if image.profile is not None and image.profile.generate_photo_keywords:
            _enqueue(generate_image_keywords, image_id)

        from urbanlens.dashboard.services.photos.redata_relevance import queue_photo_submission

        queue_photo_submission(image)

    update_task_progress(self, current=1, total=1, message="Upload metadata processed")
    return True


@shared_task(queue=SANDBOX_QUEUE)
def render_media_preview(source_cache_key: str, preview_cache_key: str, ttl: int, failure_ttl: int) -> bool:
    """Decode one cached provider file into a browser-renderable preview.

    The decode half of the two media-preview endpoints
    (``controllers/media_preview.MediaPreviewView`` and
    ``controllers/pin.RedataMediaProxyMixin``), which used to run
    ``render_preview`` - Pillow, and poppler for a PDF - inside the web
    process. Those are provider bytes, not the app's, and a decoder bug in
    them is the whole reason this queue exists.

    Neither takes nor returns bytes. The source cap is 60MB
    (``MAX_PREVIEW_SOURCE_BYTES``), which is too big for a broker message *and*
    too big for the cache - that is the same 512MB Valkey the broker, sessions
    and Channels share. It travels on the media volume instead
    (``previews.stage_preview_source``), with only a small descriptor in the
    cache. A caller that already had its source cached passes the pair
    directly; both shapes are accepted.

    No retry - a preview is disposable, and the caller falls back to an icon
    tile.

    Args:
        source_cache_key: Key holding a staged-source descriptor, or a
            ``(bytes, content_type)`` pair.
        preview_cache_key: Key to write the result (or the failure sentinel) to.
        ttl: Seconds to cache a successful render.
        failure_ttl: Seconds to cache the failure sentinel.

    Returns:
        True when a preview was produced and cached.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.media.previews import UNPREVIEWABLE, discard_preview_source, load_preview_source, render_preview

    descriptor = cache.get(source_cache_key)
    if descriptor is None:
        # Expired between the caller writing it and this running. Nothing is
        # cached either way: a retry would only re-read the same miss, and
        # marking it UNPREVIEWABLE would blacklist a perfectly good document.
        logger.info("Preview source %s was gone before it could be rendered", source_cache_key)
        return False

    try:
        source = load_preview_source(descriptor) if isinstance(descriptor, dict) else descriptor
        if source is None:
            logger.info("Staged preview source for %s was gone before it could be read", source_cache_key)
            return False

        preview = render_preview(*source)
    finally:
        # The staged file exists only for this hand-off; leaving it would grow
        # the media volume by one copy of every previewed document.
        if isinstance(descriptor, dict):
            discard_preview_source(descriptor)
            cache.delete(source_cache_key)

    if preview is None:
        cache.set(preview_cache_key, UNPREVIEWABLE, failure_ttl)
        return False
    cache.set(preview_cache_key, preview, ttl)
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=SANDBOX_QUEUE)
def generate_image_thumbnails(image_ids: list[int]) -> int:
    """Fill in missing grid thumbnails for already-stored photos.

    Called from :func:`backfill_image_thumbnails` (and nowhere on a request
    path). New uploads are thumbnailed inside :func:`process_image_upload`;
    this exists so a failure there, or a row created before thumbnails
    existed, still gets a preview. Each id is independent: a failure on one
    does not skip the rest.

    Args:
        image_ids: Primary keys of :class:`~urbanlens.dashboard.models.images.model.Image` rows.

    Returns:
        How many thumbnails were written.
    """
    from PIL.Image import DecompressionBombError as PILDecompressionBombError

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.media.images import write_image_thumbnail

    written = 0
    for image in Image.objects.filter(pk__in=image_ids, media_type=MediaKind.PHOTO):
        try:
            if write_image_thumbnail(image):
                image.save(update_fields=["thumbnail", "updated"])
                written += 1
        except (OSError, ValueError, PILDecompressionBombError) as exc:
            logger.warning("Thumbnail generation failed for image %s: %s", image.pk, exc, exc_info=True)
    return written


#: Cache key for the exclusive pk cursor :func:`backfill_image_thumbnails` walks.
_THUMBNAIL_BACKFILL_CURSOR_KEY = "image-thumbnail-backfill-cursor"
#: Long enough that a beat outage does not restart a half-finished walk at pk 0.
_THUMBNAIL_BACKFILL_CURSOR_TTL = 7 * 24 * 60 * 60


#: How long a row may sit ``pending_scan`` before the sweep assumes its task was
#: lost rather than merely slow.
#:
#: Sized against the worst legitimate case, not the typical one, because the
#: cost of being wrong is asymmetric: re-queueing a row whose task is still
#: running starts a *second* ffmpeg pass over the same file on a two-slot
#: worker, while waiting longer only delays a recovery nobody is watching. The
#: clock starts at row creation, before the task is even queued, so the budget
#: is queue wait + ``CELERY_TASK_TIME_LIMIT`` (1h) + the retry ladder
#: (60+120+240s, about 7 minutes) - and a backed-up sandbox queue is exactly the
#: condition under which a slow row and a lost one look alike.
STALLED_UPLOAD_AGE = timedelta(hours=6)

#: Bound on one sweep, so a large backlog is drained over several ticks rather
#: than dumped onto the sandbox worker at once.
STALLED_UPLOAD_BATCH = 100


@shared_task
def sweep_stale_preview_sources() -> int:
    """Remove staged preview sources whose render never ran.

    ``render_media_preview`` deletes its own source, so anything this finds is
    from an enqueue that failed - the broker was down when a tile was requested
    - leaving a file on the media volume nothing will ever read.

    Returns:
        How many files were removed.
    """
    from urbanlens.dashboard.services.media.previews import sweep_preview_sources

    removed = sweep_preview_sources()
    if removed:
        logger.info("Removed %s orphaned preview source file(s)", removed)
    return removed


@shared_task
def requeue_stalled_pending_uploads(limit: int | None = None) -> int:
    """Re-enqueue uploads whose processing task never ran.

    ``safely_enqueue_task`` returns None rather than raising when the broker is
    unreachable, and every upload path treats that as "nothing more to do" - so
    a few seconds of Valkey trouble used to cost a photo its downscale. Now it
    costs the row its *visibility*: ``pending_scan`` stays set, and nobody but
    the uploader can see it. Without a sweep that is permanent, and the uploader
    sees an upload that succeeded and then never appeared to anyone.

    Deliberately not on the sandbox queue - it enqueues, it does not parse.

    Args:
        limit: Override the batch size. Beat does not pass this; tests do.

    Returns:
        How many rows were re-enqueued.
    """
    from django.utils import timezone

    from urbanlens.dashboard.models.images.model import Image, QuotaExemption
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.photos.photo_enrichment import enriched_max_dimension

    cutoff = timezone.now() - STALLED_UPLOAD_AGE
    batch = STALLED_UPLOAD_BATCH if limit is None else max(1, limit)
    # Deduplicated siblings are deliberately excluded. They point at a file
    # another row owns, and running this task on one would re-encode that shared
    # file underneath its original - the exact thing attach_deduped_copy exists
    # to avoid. They are cleared by _sync_deduped_siblings when their original
    # is processed, which is what re-queueing the original below arranges.
    #
    # Oldest first: a row that has been invisible longest is the one whose
    # uploader has been waiting longest.
    # `upload_failed_at__isnull=True` is what stops this being a loop. A row
    # whose child keeps dying used to be re-fed every tick, costing a sandbox
    # slot each time on a file that has already killed a worker, and never
    # telling the uploader anything - see services.media.upload_failures.
    stalled = list(
        Image.objects.filter(pending_scan=True, created__lt=cutoff, upload_failed_at__isnull=True).exclude(quota_exempt_reason=QuotaExemption.DEDUPLICATED).order_by("created").values_list("pk", "profile_id", "source", "upload_sweep_attempts")[:batch]
    )
    if not stalled:
        return _clear_orphaned_dedup_siblings(cutoff)

    from django.db.models import F

    from urbanlens.dashboard.services.media.upload_failures import MAX_SWEEP_ATTEMPTS, record_upload_processing_failure

    requeued = 0
    for image_id, profile_id, source, attempts in stalled:
        if attempts >= MAX_SWEEP_ATTEMPTS:
            record_upload_processing_failure(image_id, "This photo could not be processed after several attempts. Retry it, or discard it and upload again.")
            continue
        # A profile-less row is a provider photo, and its cap lived only at the
        # call site that created it - recovered here from its source so the
        # reprocessed file matches what it should have been, rather than
        # falling back to the generic default.
        max_dimension = None if profile_id is not None else enriched_max_dimension(source)
        Image.objects.filter(pk=image_id).update(upload_sweep_attempts=F("upload_sweep_attempts") + 1)
        safely_enqueue_task(process_image_upload, image_id, max_dimension)
        requeued += 1
    logger.info("Re-enqueued %s upload(s) still pending after %s (%s gave up)", requeued, STALLED_UPLOAD_AGE, len(stalled) - requeued)
    return requeued


@shared_task
def discard_unretried_failed_uploads(limit: int | None = None) -> int:
    """Throw away failed uploads nobody came back for.

    The second half of the rule: the user retries, and if they do not, the
    upload is discarded. Without this a photo whose processing died sits
    ``pending_scan`` forever - invisible to everyone but its owner, counted
    against their storage quota, and holding bytes no scan ever cleared.

    The failure row itself is kept. Its filename is how the uploader recognises
    which picture went away, and once the bytes are gone it is the only trace.

    Deliberately not on the sandbox queue - it deletes files, it does not parse
    them.

    Args:
        limit: Override the batch size. Beat does not pass this; tests do.

    Returns:
        How many uploads were discarded.
    """
    from django.utils import timezone

    from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoUploadFailure
    from urbanlens.dashboard.services.media.upload_failures import UNRETRIED_DISCARD_AGE, discard_failed_upload

    cutoff = timezone.now() - UNRETRIED_DISCARD_AGE
    batch = STALLED_UPLOAD_BATCH if limit is None else max(1, limit)
    stale = list(
        PhotoUploadFailure.objects.filter(status=PhotoIssueStatus.PENDING, image__isnull=False, image__upload_failed_at__lt=cutoff).select_related("image").order_by("created")[:batch],
    )
    for failure in stale:
        discard_failed_upload(failure)
    if stale:
        logger.info("Discarded %s failed upload(s) untouched for %s", len(stale), UNRETRIED_DISCARD_AGE)
    return len(stale)


def _clear_orphaned_dedup_siblings(cutoff) -> int:
    """Clear dedup siblings whose original is gone, so they are not stuck forever.

    A sibling is normally cleared by ``_sync_deduped_siblings`` when its
    original finishes processing. If the original was deleted first (the user
    removed it, or it was rejected in a way that missed this sibling), nothing
    is left to do that - and unlike a real upload the sibling must not be run
    through the task itself, because the file it points at belongs to somebody
    else's row.

    Args:
        cutoff: Only siblings created before this are considered.

    Returns:
        How many rows were cleared.
    """
    from django.db.models import Exists, OuterRef

    from urbanlens.dashboard.models.images.model import Image, QuotaExemption

    has_source_row = Image.objects.filter(profile_id=OuterRef("profile_id"), checksum=OuterRef("checksum")).exclude(pk=OuterRef("pk")).exclude(quota_exempt_reason=QuotaExemption.DEDUPLICATED)
    orphaned = Image.objects.filter(pending_scan=True, created__lt=cutoff, quota_exempt_reason=QuotaExemption.DEDUPLICATED).annotate(has_source=Exists(has_source_row)).filter(has_source=False)
    cleared = orphaned.update(pending_scan=False)
    if cleared:
        logger.info("Cleared %s dedup sibling(s) whose original no longer exists", cleared)
    return cleared


@shared_task
def backfill_image_thumbnails(limit: int | None = None) -> int:
    """Enqueue a bounded batch of photos that still lack a grid thumbnail.

    Scheduled hourly (see ``CELERY_BEAT_SCHEDULE``). Walks the table by
    primary key so a handful of unreadable files cannot stall the rest of
    the library: this tick's last id is the next tick's exclusive floor,
    and an exhausted cursor resets on a later tick rather than re-queueing
    the same in-flight batch immediately.

    Args:
        limit: Override the default batch size. Beat does not pass this;
            tests do, so a small library can exercise wrapping without
            inserting fifty rows.

    Returns:
        How many image ids were queued (0 when the library is caught up,
        or this tick only reset the cursor).
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.images import THUMBNAIL_BACKFILL_BATCH, photos_missing_thumbnails

    batch = THUMBNAIL_BACKFILL_BATCH if limit is None else max(1, limit)
    cursor = int(cache.get(_THUMBNAIL_BACKFILL_CURSOR_KEY) or 0)
    ids = photos_missing_thumbnails(after_pk=cursor, limit=batch)
    if not ids:
        if cursor:
            cache.delete(_THUMBNAIL_BACKFILL_CURSOR_KEY)
            logger.info("Thumbnail backfill wrapped; next tick resumes from the start")
        return 0

    cache.set(_THUMBNAIL_BACKFILL_CURSOR_KEY, ids[-1], _THUMBNAIL_BACKFILL_CURSOR_TTL)
    safely_enqueue_task(generate_image_thumbnails, ids)
    logger.info("Thumbnail backfill queued %d photo(s) after pk %s", len(ids), cursor)
    return len(ids)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=SANDBOX_QUEUE)
def generate_image_marker_thumbnails(image_ids: list[int]) -> int:
    """Fill in missing map-marker thumbnails for already-stored photos.

    The marker-thumbnail mirror of :func:`generate_image_thumbnails` - see
    its docstring for why this exists alongside upload-time generation.

    Args:
        image_ids: Primary keys of :class:`~urbanlens.dashboard.models.images.model.Image` rows.

    Returns:
        How many marker thumbnails were written.
    """
    from PIL.Image import DecompressionBombError as PILDecompressionBombError

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.media.images import write_image_marker_thumbnail

    written = 0
    for image in Image.objects.filter(pk__in=image_ids, media_type=MediaKind.PHOTO):
        try:
            if write_image_marker_thumbnail(image):
                image.save(update_fields=["marker_thumbnail", "updated"])
                written += 1
        except (OSError, ValueError, PILDecompressionBombError) as exc:
            logger.warning("Marker thumbnail generation failed for image %s: %s", image.pk, exc, exc_info=True)
    return written


#: Cache key for the exclusive pk cursor :func:`backfill_image_marker_thumbnails` walks.
_MARKER_THUMBNAIL_BACKFILL_CURSOR_KEY = "image-marker-thumbnail-backfill-cursor"
#: Same rationale as _THUMBNAIL_BACKFILL_CURSOR_TTL.
_MARKER_THUMBNAIL_BACKFILL_CURSOR_TTL = 7 * 24 * 60 * 60


@shared_task
def backfill_image_marker_thumbnails(limit: int | None = None) -> int:
    """Enqueue a bounded batch of photos that still lack a map-marker thumbnail.

    The marker-thumbnail mirror of :func:`backfill_image_thumbnails` - see its
    docstring for the walk/cursor behaviour, which this copies exactly.

    Args:
        limit: Override the default batch size. Beat does not pass this;
            tests do.

    Returns:
        How many image ids were queued (0 when the library is caught up, or
        this tick only reset the cursor).
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.images import THUMBNAIL_BACKFILL_BATCH, photos_missing_marker_thumbnails

    batch = THUMBNAIL_BACKFILL_BATCH if limit is None else max(1, limit)
    cursor = int(cache.get(_MARKER_THUMBNAIL_BACKFILL_CURSOR_KEY) or 0)
    ids = photos_missing_marker_thumbnails(after_pk=cursor, limit=batch)
    if not ids:
        if cursor:
            cache.delete(_MARKER_THUMBNAIL_BACKFILL_CURSOR_KEY)
            logger.info("Marker thumbnail backfill wrapped; next tick resumes from the start")
        return 0

    cache.set(_MARKER_THUMBNAIL_BACKFILL_CURSOR_KEY, ids[-1], _MARKER_THUMBNAIL_BACKFILL_CURSOR_TTL)
    safely_enqueue_task(generate_image_marker_thumbnails, ids)
    logger.info("Marker thumbnail backfill queued %d photo(s) after pk %s", len(ids), cursor)
    return len(ids)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=SANDBOX_QUEUE)
def generate_image_analysis_thumbnails(image_ids: list[int]) -> int:
    """Fill in missing analysis copies for already-stored photos.

    The analysis-copy mirror of :func:`generate_image_thumbnails`. It matters
    more than the display-thumbnail sweeps: keywording refuses to decode an
    upload itself, so a photo with no analysis copy gets no AI keywords until
    this runs. Re-enqueues keywording for every row it fixes, so a write that
    failed during upload still ends in keywords rather than needing a manual
    sweep.

    Args:
        image_ids: Primary keys of :class:`~urbanlens.dashboard.models.images.model.Image` rows.

    Returns:
        How many analysis copies were written.
    """
    from PIL.Image import DecompressionBombError as PILDecompressionBombError

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.images import write_image_analysis_thumbnail

    written = 0
    for image in Image.objects.filter(pk__in=image_ids, media_type=MediaKind.PHOTO):
        try:
            if not write_image_analysis_thumbnail(image):
                continue
        except (OSError, ValueError, PILDecompressionBombError) as exc:
            logger.warning("Analysis thumbnail generation failed for image %s: %s", image.pk, exc, exc_info=True)
            continue
        image.save(update_fields=["analysis_thumbnail", "updated"])
        written += 1
        # Same gate process_image_upload applies - a profile-less row has no
        # uploader who opted in, and keywording it would spend a billed call
        # nobody asked for.
        if image.profile is not None and image.profile.generate_photo_keywords:
            safely_enqueue_task(generate_image_keywords, image.pk)
    return written


#: Cache key for the exclusive pk cursor :func:`backfill_image_analysis_thumbnails` walks.
_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_KEY = "image-analysis-thumbnail-backfill-cursor"
#: Long enough that a beat outage does not restart a half-finished walk at pk 0.
_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_TTL = 7 * 24 * 60 * 60


@shared_task
def backfill_image_analysis_thumbnails(limit: int | None = None) -> int:
    """Queue analysis-copy generation for photos that still lack one.

    The analysis-copy mirror of :func:`backfill_image_thumbnails` - see its
    docstring for the walk/cursor behaviour, which this copies exactly. This
    is also what backfills the library for the field's own introduction: every
    photo uploaded before it existed has no analysis copy and therefore no AI
    keywords until this sweep reaches it.

    Args:
        limit: Override the default batch size. Beat does not pass this;
            tests do.

    Returns:
        How many image ids were queued (0 when the library is caught up, or
        this tick only reset the cursor).
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.images import THUMBNAIL_BACKFILL_BATCH, photos_missing_analysis_thumbnails

    batch = THUMBNAIL_BACKFILL_BATCH if limit is None else max(1, limit)
    cursor = int(cache.get(_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_KEY) or 0)
    ids = photos_missing_analysis_thumbnails(after_pk=cursor, limit=batch)
    if not ids:
        if cursor:
            cache.delete(_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_KEY)
            logger.info("Analysis thumbnail backfill wrapped; next tick resumes from the start")
        return 0

    cache.set(_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_KEY, ids[-1], _ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_TTL)
    safely_enqueue_task(generate_image_analysis_thumbnails, ids)
    logger.info("Analysis thumbnail backfill queued %d photo(s) after pk %s", len(ids), cursor)
    return len(ids)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def generate_image_keywords(image_id: int) -> dict[str, int]:
    """Generate searchable keywords for an uploaded photo via keyword plugins.

    Enqueued at the end of ``process_image_upload`` (fully in the background -
    uploads never wait on it). Each enabled photo-keyword provider stores its
    own ``ImageKeyword`` rows; see ``services.photos.photo_keywords``.

    Args:
        image_id: PK of the image to keyword.

    Returns:
        Mapping of provider slug to keywords stored.
    """
    from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image

    return generate_keywords_for_image(image_id)


@shared_task
def submit_redata_photos(image_ids: list[int]) -> bool:
    """Submit photo observations to REData and cache the confidence scores it returns.

    Enqueued from ``services.photos.redata_relevance.queue_photo_submission``
    whenever a photo is uploaded, discovered from an external source
    (Google Places business photos), or materialized from the Media gallery.
    Best-effort like every other REData call site (e.g.
    ``import_immich_photos``) - a REData outage is logged and swallowed
    rather than retried, since a later submission (or the periodic photo
    itself being re-saved) will pick it up.

    Args:
        image_ids: PKs of photos to submit - filtered to actual photos
            (not video/document rows) with a resolved location.

    Returns:
        True when at least one photo was submitted.
    """
    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.photos.redata_relevance import submit_photos

    images = list(Image.objects.filter(pk__in=image_ids, media_type=MediaKind.PHOTO, location__isnull=False).select_related("location", "wiki"))
    if not images:
        return False
    submit_photos(images)
    return True


@shared_task
def submit_redata_photo_vote(image_id: int, profile_id: int, is_relevant: bool) -> bool:
    """Submit one relevance vote on a photo to REData.

    Enqueued from ``services.photos.redata_relevance.queue_relevance_vote``
    whenever a user marks a materialized photo relevant or not relevant.
    Best-effort - see ``submit_redata_photos``' docstring for why REData
    outages aren't retried here.

    Args:
        image_id: PK of the photo being voted on.
        profile_id: PK of the voting profile.
        is_relevant: True for a relevant vote, False for not-relevant.

    Returns:
        True when the vote was recorded (REData knew about this photo).
    """
    from django.utils import timezone

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    image = Image.objects.filter(pk=image_id).first()
    if image is None:
        return False

    vote = {"photo_id": str(image.uuid), "is_relevant": is_relevant, "voter_id": str(profile_id), "voted_at": timezone.now().isoformat()}
    try:
        response = RedataPhotosGateway().submit_votes([vote])
    except GatewayRequestError as exc:
        logger.warning("REData vote submission failed for image %s: %s", image_id, exc)
        return False
    return str(image.uuid) not in (response.get("unknown_photo_ids") or [])


@shared_task
def sync_redata_label_definitions(profile_ids: list[int], definitions: list[dict]) -> bool:
    """Push tag/category label definitions into every listed profile's REData taxonomy.

    Enqueued from ``services.labels.redata_suggestions.queue_label_definition_sync``/
    ``queue_label_retirement`` whenever a tag/category label is created,
    edited, reparented, or retired. Best-effort like every other REData task.

    Args:
        profile_ids: PKs of profiles whose taxonomy should receive
            ``definitions`` (a global label maps to every profile; an
            owned label maps to just its one owner).
        definitions: Definition dicts built by
            ``services.labels.redata_suggestions._label_definition``.

    Returns:
        True when at least one profile was targeted.
    """
    from urbanlens.dashboard.services.labels.redata_suggestions import sync_label_definitions

    if not profile_ids or not definitions:
        return False
    sync_label_definitions(profile_ids, definitions)
    return True


@shared_task
def sync_redata_pin_assignment(pin_id: int) -> bool:
    """Push one pin's complete current tag/category label set to REData.

    Enqueued from ``services.labels.redata_suggestions.queue_pin_assignment_sync``,
    itself called from the ``Pin.labels`` ``m2m_changed`` signal - the single
    choke point that sees every pin-tagging call site. Best-effort like
    every other REData task.

    Args:
        pin_id: PK of the pin whose label set changed.

    Returns:
        True when the pin was found and had an owning profile.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.labels.redata_suggestions import sync_pin_assignment

    pin = Pin.objects.filter(pk=pin_id).select_related("profile", "location").first()
    if pin is None or pin.profile_id is None:
        return False
    sync_pin_assignment(pin)
    return True


@shared_task(bind=True, max_retries=5, queue=SANDBOX_QUEUE)
def scan_comment_image(self, comment_id: int) -> bool:
    """Background malware-scan a newly-uploaded pin/wiki comment image.

    Runs after the comment (and its image) is already saved and visible to
    its own author only (see ``controllers.comments.start_comment_image_scan`` -
    sets ``pending_scan`` before enqueuing this) - a clamd round-trip no
    longer blocks the comment POST itself. Clears ``pending_scan`` on a clean
    result, making the comment visible to every other viewer; on an infected
    result, deletes the comment and notifies its author with their original
    text so they can try posting again (see ``_reject_comment_upload``). A
    clamd connectivity hiccup retries with backoff instead of immediately
    treating the upload as rejected.

    Args:
        comment_id: PK of the ``Comment`` whose image should be scanned.

    Returns:
        True when the scan completed and found the image clean.
    """
    from urbanlens.dashboard.models.comments.model import Comment

    comment = Comment.objects.filter(pk=comment_id, pending_scan=True).select_related("profile", "pin", "wiki__location").first()
    if comment is None or not comment.image:
        return False
    return _run_comment_image_scan(self, comment, Comment)


@shared_task(bind=True, max_retries=5, queue=SANDBOX_QUEUE)
def scan_trip_comment_image(self, comment_id: int) -> bool:
    """Background malware-scan a newly-uploaded trip comment image. Mirrors ``scan_comment_image``.

    Args:
        comment_id: PK of the ``TripComment`` whose image should be scanned.

    Returns:
        True when the scan completed and found the image clean.
    """
    from urbanlens.dashboard.models.trips.model import TripComment

    comment = TripComment.objects.filter(pk=comment_id, pending_scan=True).select_related("author", "trip").first()
    if comment is None or not comment.image:
        return False
    return _run_comment_image_scan(self, comment, TripComment)


def _run_comment_image_scan(task, comment, model) -> bool:
    """Shared body for ``scan_comment_image``/``scan_trip_comment_image`` - see either's docstring.

    Args:
        task: The bound Celery task instance (for ``self.retry``).
        comment: The ``Comment`` or ``TripComment`` row to scan.
        model: Its model class, for the ``pending_scan`` clear on success.

    Returns:
        True when the scan completed and found the image clean.
    """
    from urbanlens.dashboard.services.security.malware_scan import MalwareScanUnavailableError, malware_error_for_upload

    try:
        malware_error = malware_error_for_upload(comment.image)
    except MalwareScanUnavailableError as exc:
        if task.request.retries >= task.max_retries:
            logger.exception("Malware scan permanently unavailable for comment %s after %s retries", comment.pk, task.request.retries)
            _reject_comment_upload(comment, "Our antivirus scanner was unavailable and your photo could not be scanned.")
            return False
        raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc

    if malware_error:
        _reject_comment_upload(comment, malware_error)
        return False

    model.objects.filter(pk=comment.pk).update(pending_scan=False)
    return True


def _reject_comment_upload(comment, reason: str) -> None:
    """Notify a comment's author their upload was rejected, and remove the comment.

    The comment (text included) never went visible to anyone but its own
    author (see ``pending_scan``), so removing it outright and handing the
    author their own text back via the notification is simpler than leaving
    a permanently-broken "image rejected" placeholder behind - they can copy
    the text from the notification and try posting again. Explicitly deletes
    the stored image file too (not just the DB row) - this path is also hit
    for a confirmed-infected upload, which shouldn't linger in storage just
    because nothing points at it anymore.

    Args:
        comment: The ``Comment`` or ``TripComment`` to remove.
        reason: The user-facing reason the upload was rejected.
    """
    from django.urls import NoReverseMatch, reverse

    from urbanlens.dashboard.models.notifications.meta import NotificationType
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    recipient = getattr(comment, "profile", None) or getattr(comment, "author", None)
    text_preview = (comment.text or "").strip() or "(no text)"
    url = ""
    try:
        if getattr(comment, "pin_id", None):
            url = reverse("pin.details", kwargs={"pin_slug": comment.pin.slug or str(comment.pin.uuid)})
        elif getattr(comment, "wiki_id", None) and comment.wiki.location_id:
            url = reverse("location.wiki", kwargs={"location_slug": comment.wiki.location.slug or str(comment.wiki.location.uuid)})
        elif getattr(comment, "trip_id", None):
            url = reverse("trips.detail", kwargs={"trip_slug": comment.trip.slug})
    except NoReverseMatch:
        logger.warning("Could not build a comment URL while notifying about a rejected upload (comment %s)", comment.pk)

    if recipient is not None:
        NotificationLog.objects.notify(
            profile=recipient,
            notification_type=NotificationType.COMMENT_UPLOAD_FAILED,
            title="Your comment could not be posted",
            message=f'{reason} Your comment text: "{text_preview}". You can try posting it again.',
            url=url,
        )
    if comment.image:
        comment.image.delete(save=False)
    comment.delete()


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
            ``PinSuggestion`` (see ``services.pins.pin_suggestions.accept_pin_suggestion``),
            maps an asset id to the specific ``PinVisit`` (already created for
            that suggestion's dates) it should attach to instead of getting a
            fresh one of its own. Omitted assets, and every asset when this is
            ``None`` (the manual "Import from Immich" picker path), fall back
            to creating their own visit via ``log_visit_on_pin``.

    Returns:
        Counts of imported/skipped/failed assets, surfaced to the polling UI.
    """
    import io

    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.images.model import Image, ImageSource
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.visits.model import PinVisit
    from urbanlens.dashboard.services.apis.immich import ImmichGateway
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.media.images import compute_checksum
    from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload
    from urbanlens.dashboard.services.memories.photos import log_visit_on_pin

    counts = {"imported": 0, "skipped": 0, "failed": 0}
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = ImmichAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Immich connection no longer exists.")
        return counts

    gateway = ImmichGateway(account=account)
    existing_checksums = set(Image.objects.filter(pin=pin, profile=profile).values_list("checksum", flat=True))
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
        if checksum in existing_checksums:
            counts["skipped"] += 1
            continue

        with per_profile_upload_lock(profile):
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
                source=ImageSource.IMMICH,
                checksum=checksum,
                file_size=len(content),
                source_url=account.asset_web_url(asset_id),
                visit=target_visit,
                # Same quarantine an ordinary upload gets: these are raw bytes from a
                # third-party API, stored unread, and visible the moment the row exists.
                # process_image_upload (enqueued below) scans, strips and clears it.
                pending_scan=True,
            )
        if target_visit is None:
            log_visit_on_pin(profile, image, pin)
        safely_enqueue_task(process_image_upload, image.pk)
        existing_checksums.add(checksum)
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
    through ``services.pins.pin_suggestions.ingest_location_hits``, which matches
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
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.pins.pin_suggestions import LocationHit, ingest_location_hits
    from urbanlens.dashboard.services.visits.visits import visit_logging_allowed

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
        NotificationLog.objects.notify(
            profile=profile,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.INFO,
            title="Found new locations from your Immich library",
            message=(f"Your Immich library scan found {summary.new_pin_suggestions} possible new pin(s) and {summary.matched_suggestions} visit(s) to pins you already have. Review them in Memories."),
        )
    update_task_progress(self, current=scanned, total=max(scanned, 1), message=f"Scan complete - found {total_suggestions} suggestion(s).")
    return result


#: How many consecutive whole-batch REData request failures (network error,
#: non-200, unparseable body - see CidResolutionResult.request_failed) this
#: task tolerates before giving up. Deliberately separate from
#: max_retries=None: a batch that's genuinely still resolving on REData's own
#: end (result.pending with request_failed=False) should keep retrying
#: indefinitely as it makes progress, but a REData outage that fails every
#: single attempt would otherwise retry forever too, with no cap and no
#: notification - unlike the auth_failed case, which already stops.
_MAX_CONSECUTIVE_REDATA_FAILURES = 5

#: How many consecutive retries may report the exact same pending cids with
#: zero of them resolved before this task gives up on them. Distinct from
#: _MAX_CONSECUTIVE_REDATA_FAILURES: REData's own cid-resolution cache policy
#: (StaggeredCachePolicy, see ../REData's core.services.staggered_cache) has a
#: hard minimum-TTL floor - once a cid has been checked at all, REData won't
#: queue another resolution attempt for it for weeks, but keeps reporting it
#: as "pending" (HTTP 200, no error) every time it's asked, since it's neither
#: resolved nor confirmed unresolvable yet. Without this cap, a batch that
#: falls into that state retries every ~120s forever even though REData is
#: responding successfully - see docs/notes/ai/completed.md for the incident
#: this was diagnosed from. A batch still making real progress (even one cid
#: resolved per round) never trips this, since the counter resets whenever
#: the pending set shrinks.
_MAX_CONSECUTIVE_NO_PROGRESS_RETRIES = 5

#: How long a deferred-lookup batch keeps trying before it gives up and turns
#: into PinImportFailure rows for the user to resolve by hand.
#:
#: The counters above choose only *how far apart* retries are spaced; this
#: deadline is the only thing that ends the batch. Letting the counters end it
#: caps attempts at ~10 minutes, and a REData cid that resolves an hour later -
#: the common case for a large import - then produces hundreds of import
#: failures for work that would have completed on its own.
_DEFERRED_LOOKUP_DEADLINE = timedelta(days=2)

#: Seconds between retries, indexed by attempt number (the last entry repeats).
#: Front-loaded because a batch waiting on a rate limit usually clears in
#: minutes, then widening sharply so two days costs ~16 attempts instead of
#: ~1,400 - REData will not re-queue a cid it has already checked for weeks, so
#: asking it every two minutes for two days is pure load with no new answer.
_DEFERRED_RETRY_SCHEDULE = (120, 120, 120, 300, 600, 1800, 3600, 7200, 14400, 21600)


def _deferred_retry_countdown(attempt: int) -> int:
    """Seconds to wait before retry number ``attempt`` (0-based).

    Args:
        attempt: How many retries this batch has already made.

    Returns:
        The gap to the next attempt, in seconds.
    """
    return _DEFERRED_RETRY_SCHEDULE[min(attempt, len(_DEFERRED_RETRY_SCHEDULE) - 1)]


def _deferred_deadline_passed(started_at: str | None) -> bool:
    """Whether this batch has been retrying past :data:`_DEFERRED_LOOKUP_DEADLINE`.

    Args:
        started_at: ISO timestamp of the batch's first attempt, or None on that
            first attempt (also None for any batch missing this field, which
            gets a fresh two-day window rather than being failed immediately).

    Returns:
        True when the batch should stop retrying.
    """
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if timezone.is_naive(started):
        # The only producer stamps an aware `timezone.now().isoformat()`, but a
        # replayed or hand-enqueued message can carry a naive one, and subtracting
        # it raises TypeError rather than the ValueError caught above - killing the
        # task instead of retiring the batch. Assuming the active timezone is also
        # better than treating it as unparseable: falling back to False would let
        # the batch retry forever, which is the exact thing the deadline exists to
        # stop.
        started = timezone.make_aware(started)
    return timezone.now() - started >= _DEFERRED_LOOKUP_DEADLINE


def _place_resolved_pins(result, deferred_lists: list[dict], *, profile, auto_tag: bool) -> tuple[int, int, int]:
    """Place every pin in ``deferred_lists`` whose cid this round actually resolved.

    Called on *every* round of ``resolve_deferred_pin_locations``, not only the final
    one - see that task's own docstring ("places whatever resolves now"): a round with
    pending cids must still place whatever did resolve, not wait for the batch to
    fully clear.

    The three buckets are handled differently and the distinction is the whole point:

    - ``result.resolved`` - place the pin.
    - ``result.unresolvable`` - a terminal "no such location" answer; record a
      ``NO_LOCATION_FOUND`` failure card so the user can fix it by hand.
    - anything else (still pending) - do nothing at all. A pending cid is unfinished
      work, not a failure, and writing a card for it here would put a transient state
      into a table with a unique ``(profile, cid)`` constraint and no expiry.

    Args:
        result: The ``CidResolutionResult`` for this round.
        deferred_lists: The lists being imported, in import_preview_streaming's shape.
        profile: The importing profile.
        auto_tag: Whether to enqueue AI category suggestion for newly-created pins.

    Returns:
        ``(created, exists, skipped)`` for this round only - each retry is a fresh task
        invocation, so these are per-round counts, not per-batch totals.
    """
    from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.location import Location
    from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailureReason
    from urbanlens.dashboard.services.apis.locations.google.maps import _create_pin_from_confirmed
    from urbanlens.dashboard.services.pins.pin_import_failures import auto_resolve_pin_import_failure_for_cid, record_pin_import_failure

    created_count = exists_count = skipped_count = 0
    for lst in deferred_lists:
        stem = lst.get("stem", "")
        list_label_ids = lst.get("label_ids") or []
        create_category = bool(lst.get("create_category", False))
        list_labels = list(Label.objects.filter(id__in=list_label_ids)) if list_label_ids else []

        category_label = None
        if create_category and stem:
            category_label, _ = Label.objects.get_or_create(
                profile=profile,
                name__iexact=stem,
                # kind belongs in the lookup, not defaults: with it only in
                # defaults, the get half matches any kind, so a same-named
                # *tag* was returned and used as the list's category (see
                # PROBLEMS.md, label lookups by name alone).
                kind=KIND_CATEGORY,
                defaults={"name": stem},
            )

        for pin_dict in lst.get("pins", []):
            cid = pin_dict["cid"]
            coords = result.resolved.get(cid)
            if coords is None:
                if cid in result.unresolvable:
                    record_pin_import_failure(
                        profile,
                        cid,
                        name=pin_dict.get("name", ""),
                        description=pin_dict.get("description", ""),
                        reason=PinImportFailureReason.NO_LOCATION_FOUND,
                    )
                    skipped_count += 1
                continue

            # Re-check now, not just at defer time: an earlier pin in this
            # same batch referencing the same cid (saved to two lists) may
            # have just linked/created its Location.
            location = Location.objects.by_cid(cid).first()
            pin, created = _create_pin_from_confirmed(
                pin_dict,
                location=location,
                latitude=coords[0],
                longitude=coords[1],
                user_profile=profile,
                list_labels=list_labels,
                category_label=category_label,
                auto_tag=auto_tag,
            )
            if pin:
                auto_resolve_pin_import_failure_for_cid(profile, cid, pin)
                if created:
                    created_count += 1
                else:
                    exists_count += 1
            else:
                skipped_count += 1

    return created_count, exists_count, skipped_count


@shared_task(bind=True, max_retries=None)
def resolve_deferred_pin_locations(
    self,
    profile_id: int,
    deferred_lists: list[dict],
    auto_tag: bool = True,
    original_total: int | None = None,
    consecutive_request_failures: int = 0,
    consecutive_no_progress: int = 0,
    started_at: str | None = None,
) -> dict[str, int]:
    """Place pins whose Google Maps CID needed a live lookup to be accurate.

    Queued by ``GoogleMapsGateway.import_preview_streaming`` for any
    confirmed pin whose cid had neither an existing Location nor a cached
    Places lookup - see that method's docstring for why the preview's own
    lat/lng can't be trusted for these. Resolves every cid in one batch via
    ``services.apis.locations.cid_resolution.resolve_cids`` (REData if
    configured, else Google Places directly), places whatever resolves now,
    and retries later - with the resolved subset already placed and pruned
    from the retry args, so a retry never redoes finished work - if anything
    is still pending on a rate limit or a REData outage. Reports a summary via
    NotificationLog once every cid is either placed or confirmed unresolvable.

    Args:
        profile_id: PK of the importing profile.
        deferred_lists: Same shape as import_preview_streaming's
            confirmed_lists, restricted to pins needing a live cid lookup -
            shrinks on each retry to just what's still unresolved.
        auto_tag: Whether to enqueue AI category suggestion for newly-created pins.
        original_total: Total pin count across the *first* call, before any
            retry narrowed deferred_lists - carried through retries purely so
            progress reporting stays relative to the whole job, not just
            whatever's left. Defaults to this call's own pin count when unset
            (i.e. on the first, non-retry invocation).
        consecutive_request_failures: How many retries in a row have hit a
            whole-batch REData request failure with zero progress - see
            ``_MAX_CONSECUTIVE_REDATA_FAILURES``. Reset to 0 by any call that
            isn't a request failure (including one that still leaves cids
            pending on REData's own end), so a flaky-then-recovering REData
            never accumulates toward the cap.
        consecutive_no_progress: How many retries in a row have come back
            with the exact same cids pending and none newly resolved - see
            ``_MAX_CONSECUTIVE_NO_PROGRESS_RETRIES``. Reset to 0 the moment
            any cid resolves or is confirmed unresolvable, so a batch that's
            still working its way through REData's queue never accumulates
            toward the cap - only one that's genuinely stopped moving does.

    Returns:
        Summary counts (created/exists/skipped).
    """
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailureReason
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.locations.cid_resolution import resolve_cids
    from urbanlens.dashboard.services.pins.pin_import_failures import record_pin_import_failure

    empty = {"created": 0, "exists": 0, "skipped": 0}
    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        logger.info("resolve_deferred_pin_locations: profile %s no longer exists", profile_id)
        return empty

    all_cids = [pin["cid"] for lst in deferred_lists for pin in lst.get("pins", [])]
    if not all_cids:
        return empty
    pin_dict_by_cid = {pin["cid"]: pin for lst in deferred_lists for pin in lst.get("pins", [])}
    # A cid whose pin dict still carries the Google Maps URL it was parsed from
    # (e.g. a Takeout CSV import) - passed through to REData, which resolves
    # via a place's own URL faster and more reliably than the bare cid alone.
    urls_by_cid = {cid: pin["maps_url"] for cid, pin in pin_dict_by_cid.items() if pin.get("maps_url")}
    total = original_total if original_total is not None else len(all_cids)

    update_task_progress(self, current=total - len(all_cids), total=total, message=f"Fetching precise locations for {len(all_cids)} pin(s)...")

    result = resolve_cids(all_cids, urls_by_cid=urls_by_cid)

    if result.auth_failed:
        logger.error("resolve_deferred_pin_locations: REData rejected the API key resolving %d cid(s) for profile %s - not retrying.", len(all_cids), profile_id)
        for cid in all_cids:
            record_pin_import_failure(
                profile, cid, name=pin_dict_by_cid[cid].get("name", ""), description=pin_dict_by_cid[cid].get("description", ""), maps_url=pin_dict_by_cid[cid].get("maps_url", "") or "", reason=PinImportFailureReason.LOOKUP_ERROR
            )
        NotificationLog.objects.notify(
            profile=profile,
            status=Status.UNREAD,
            importance=Importance.HIGH,
            notification_type=NotificationType.ERROR,
            title="Location lookup was denied",
            message=(
                f"{len(all_cids)} pin(s) needed a live location lookup that was denied by a permission error and won't be retried automatically. "
                "This isn't a problem with your import - review them on the Locations page to enter an address or coordinates yourself."
            ),
            url=reverse("memories.locations"),
        )
        update_task_progress(self, current=total, total=total, message="Failed: location lookup was denied.")
        return {"created": 0, "exists": 0, "skipped": len(all_cids)}

    consecutive_request_failures = consecutive_request_failures + 1 if result.request_failed else 0
    if _deferred_deadline_passed(started_at) and consecutive_request_failures:
        logger.error(
            "resolve_deferred_pin_locations: REData failed %d consecutive attempts resolving %d cid(s) for profile %s - giving up.",
            consecutive_request_failures,
            len(all_cids),
            profile_id,
        )
        for cid in all_cids:
            record_pin_import_failure(
                profile, cid, name=pin_dict_by_cid[cid].get("name", ""), description=pin_dict_by_cid[cid].get("description", ""), maps_url=pin_dict_by_cid[cid].get("maps_url", "") or "", reason=PinImportFailureReason.LOOKUP_ERROR
            )
        NotificationLog.objects.notify(
            profile=profile,
            status=Status.UNREAD,
            importance=Importance.HIGH,
            notification_type=NotificationType.ERROR,
            title="Location lookup service is unavailable",
            message=(
                f"{len(all_cids)} pin(s) needed a live location lookup, but the lookup service has been unreachable and won't be retried automatically. "
                "This isn't a problem with your import - review them on the Locations page to enter an address or coordinates yourself."
            ),
            url=reverse("memories.locations"),
        )
        update_task_progress(self, current=total, total=total, message="Failed: location lookup service unreachable.")
        return {"created": 0, "exists": 0, "skipped": len(all_cids)}

    if result.pending:
        if result.request_failed:
            # Already tracked by consecutive_request_failures above - a failed
            # request trivially leaves every cid pending, which isn't the
            # "REData responded but nothing moved" case this counter targets.
            consecutive_no_progress = 0
        else:
            consecutive_no_progress = consecutive_no_progress + 1 if len(result.pending) == len(all_cids) else 0

        if _deferred_deadline_passed(started_at):
            logger.error(
                "resolve_deferred_pin_locations: %d cid(s) for profile %s made no progress across %d consecutive retries - giving up.",
                len(all_cids),
                profile_id,
                consecutive_no_progress,
            )
            for cid in all_cids:
                record_pin_import_failure(
                    profile, cid, name=pin_dict_by_cid[cid].get("name", ""), description=pin_dict_by_cid[cid].get("description", ""), maps_url=pin_dict_by_cid[cid].get("maps_url", "") or "", reason=PinImportFailureReason.LOOKUP_STALLED
                )
            NotificationLog.objects.notify(
                profile=profile,
                status=Status.UNREAD,
                importance=Importance.HIGH,
                notification_type=NotificationType.ERROR,
                title="Location lookup is taking longer than expected",
                message=(
                    f"{len(all_cids)} pin(s) needed a live location lookup that hasn't made progress in a while and won't be retried automatically for some time. "
                    "This isn't a problem with your import - review them on the Locations page to enter an address or coordinates yourself."
                ),
                url=reverse("memories.locations"),
            )
            update_task_progress(self, current=total, total=total, message="Failed: location lookups stalled.")
            return {"created": 0, "exists": 0, "skipped": len(all_cids)}

        # Place whatever DID resolve this round before scheduling the retry:
        # `remaining_pins` below drops every resolved cid from the retry args, so any
        # coordinate not placed here is never placed at all - not this round, not a
        # later one. A long import is mixed (some resolved, some still pending) on
        # nearly every round, so skipping this would silently lose the bulk of a large
        # batch.
        _place_resolved_pins(result, deferred_lists, profile=profile, auto_tag=auto_tag)

        pending_set = set(result.pending)
        remaining_lists = []
        for lst in deferred_lists:
            remaining_pins = [p for p in lst.get("pins", []) if p["cid"] in pending_set]
            if remaining_pins:
                remaining_lists.append({**lst, "pins": remaining_pins})

        # A Google rate limit clears on its own timescale and is unrelated to how
        # long this batch has been going, so it keeps its short fixed wait.
        if result.provider == "google_places":
            countdown, message = 65, "Waiting on Google's rate limit - resuming shortly..."
        else:
            countdown = _deferred_retry_countdown(max(consecutive_no_progress, consecutive_request_failures))
            message = "Still waiting on the location lookup service - checking back periodically..." if countdown > 600 else "Having trouble reaching the location lookup service - retrying shortly..."

        update_task_progress(self, current=total - len(result.pending), total=total, message=message)
        logger.info(
            "resolve_deferred_pin_locations: %d of %d cid(s) still pending for profile %s via %s - retrying in %ds.",
            len(result.pending),
            len(all_cids),
            profile_id,
            result.provider,
            countdown,
        )
        # throw=False: still waiting on REData is the expected, routine case, not an
        # error - raising here would go through Celery's task_retry signal (see
        # UrbanLens/celery.py), which logs a WARNING + full traceback on every single
        # retry. Scheduling silently keeps the log free of spurious tracebacks for a
        # batch that just hasn't resolved yet.
        self.retry(
            args=[profile_id, remaining_lists, auto_tag, total, consecutive_request_failures, consecutive_no_progress, started_at or timezone.now().isoformat()],
            countdown=countdown,
            max_retries=None,
            throw=False,
        )
        return {"created": 0, "exists": 0, "skipped": 0}

    created_count, exists_count, skipped_count = _place_resolved_pins(result, deferred_lists, profile=profile, auto_tag=auto_tag)

    unresolved = len(result.unresolvable)
    logger.info("resolve_deferred_pin_locations: profile %s batch resolved via %s.", profile_id, result.provider)
    NotificationLog.objects.notify(
        profile=profile,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.PIN_IMPORT_COMPLETE,
        title=f"Finished placing {created_count + exists_count} pin(s)",
        message=(f"{created_count} created · {exists_count} existed · {skipped_count} skipped" + (f" (Google has no location data for {unresolved} of them - review them on the Locations page)" if unresolved else "") + "."),
        url=reverse("memories.locations") if unresolved else reverse("map.view"),
    )
    update_task_progress(self, current=total, total=total, message="Done.")
    return {"created": created_count, "exists": exists_count, "skipped": skipped_count}


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
    from urbanlens.dashboard.models.images.model import Image, ImageSource
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.media.images import compute_checksum
    from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload
    from urbanlens.dashboard.services.memories.photos import log_visit_on_pin

    counts = {"imported": 0, "skipped": 0, "failed": 0}
    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = FlickrAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Flickr connection no longer exists.")
        return counts

    gateway = FlickrGateway(account=account)
    existing_checksums = set(Image.objects.filter(pin=pin, profile=profile).values_list("checksum", flat=True))
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
        if checksum in existing_checksums:
            counts["skipped"] += 1
            continue

        with per_profile_upload_lock(profile):
            if quota_error_for_upload(profile, len(content)):
                counts["failed"] += 1
                continue

            image = Image.objects.create(
                image=ContentFile(content, name=filename),
                pin=pin,
                location=pin.location,
                profile=profile,
                source=ImageSource.FLICKR,
                checksum=checksum,
                file_size=len(content),
                source_url=account.photo_web_url(photo_id),
                # Same quarantine an ordinary upload gets: these are raw bytes from a
                # third-party API, stored unread, and visible the moment the row exists.
                # process_image_upload (enqueued below) scans, strips and clears it.
                pending_scan=True,
            )
        log_visit_on_pin(profile, image, pin)
        safely_enqueue_task(process_image_upload, image.pk)
        existing_checksums.add(checksum)
        counts["imported"] += 1

    summary = f"Imported {counts['imported']}"
    if counts["skipped"]:
        summary += f", skipped {counts['skipped']} duplicate(s)"
    if counts["failed"]:
        summary += f", {counts['failed']} failed"
    update_task_progress(self, current=total, total=total, message=summary + ".")
    return counts


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def import_flickr_album_photos(self, target_kind: str, target_id: int, profile_id: int, album_url: str, photo_ids: list[str]) -> dict[str, int]:
    """Download selected photos from a *public* Flickr album/photoset onto a pin or wiki.

    Unlike ``import_flickr_photos`` (one user's own OAuth-connected library),
    this imports from any public album given its URL - no OAuth token
    involved, just the site's Flickr API key. No ``log_visit_on_pin`` call:
    these are someone else's public photos, not evidence the importing
    profile visited in person.

    Args:
        target_kind: ``"pin"`` or ``"wiki"`` - which FK to set on the created
            ``Image`` rows.
        target_id: PK of the target pin or wiki.
        profile_id: PK of the requesting profile.
        album_url: The Flickr album URL as submitted in the lookup step -
            re-resolved here (rather than trusting a client-supplied photo
            list) so the download URLs are fresh and the selected ids are
            verified against the real album.
        photo_ids: Flickr photo ids selected in the preview grid.

    Returns:
        Counts of imported/skipped/failed photos, surfaced to the polling UI.
    """
    import io

    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.images.model import Image, ImageSource
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.apis.flickr.public import FlickrPublicGateway, photo_web_url
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.media.images import compute_checksum
    from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload

    counts = {"imported": 0, "skipped": 0, "failed": 0}
    profile = Profile.objects.filter(pk=profile_id).first()
    pin = Pin.objects.select_related("location").filter(pk=target_id).first() if target_kind == "pin" else None
    wiki = Wiki.objects.select_related("location").filter(pk=target_id).first() if target_kind == "wiki" else None
    location = pin.location if pin is not None else (wiki.location if wiki is not None else None)
    if profile is None or location is None or (pin is None and wiki is None):
        update_task_progress(self, current=0, total=1, message="Import failed: the pin, wiki, or your profile no longer exists.")
        return counts

    try:
        album = FlickrPublicGateway().get_album(album_url)
    except (ValueError, GatewayRequestError) as exc:
        update_task_progress(self, current=0, total=1, message=f"Import failed: {exc}")
        return counts

    photos_by_id = {photo.id: photo for photo in album.photos}
    selected = [photos_by_id[photo_id] for photo_id in photo_ids if photo_id in photos_by_id]
    dedupe_filter = {"pin": pin} if pin is not None else {"wiki": wiki}
    existing_checksums = set(Image.objects.filter(profile=profile, **dedupe_filter).values_list("checksum", flat=True))
    total = len(selected)
    for index, photo in enumerate(selected):
        update_task_progress(self, current=index, total=total, message=f"Importing photo {index + 1} of {total}...")
        try:
            content, filename, _content_type = FlickrPublicGateway().download_photo(photo)
        except GatewayRequestError:
            logger.warning("import_flickr_album_photos: failed to download photo %s from album %s", photo.id, album_url, exc_info=True)
            counts["failed"] += 1
            continue

        checksum = compute_checksum(io.BytesIO(content))
        if checksum in existing_checksums:
            counts["skipped"] += 1
            continue

        with per_profile_upload_lock(profile):
            if quota_error_for_upload(profile, len(content)):
                counts["failed"] += 1
                continue

            image = Image.objects.create(
                image=ContentFile(content, name=filename),
                pin=pin,
                wiki=wiki,
                location=location,
                profile=profile,
                source=ImageSource.FLICKR,
                caption=photo.title or "",
                author=photo.author,
                source_url=photo_web_url(album.owner_nsid, photo.id),
                checksum=checksum,
                file_size=len(content),
                # Same quarantine an ordinary upload gets: these are raw bytes from a
                # third-party API, stored unread, and visible the moment the row exists.
                # process_image_upload (enqueued below) scans, strips and clears it.
                pending_scan=True,
            )
        safely_enqueue_task(process_image_upload, image.pk)
        existing_checksums.add(checksum)
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
    from urbanlens.dashboard.models.images.model import Image, ImageSource
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.photos.google import GooglePhotosGateway, media_item_web_url, session_items_cache_key
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.media.images import compute_checksum
    from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload
    from urbanlens.dashboard.services.memories.photos import log_visit_on_pin

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

    existing_checksums = set(Image.objects.filter(pin=pin, profile=profile).values_list("checksum", flat=True))
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
        if checksum in existing_checksums:
            counts["skipped"] += 1
            continue

        with per_profile_upload_lock(profile):
            if quota_error_for_upload(profile, len(content)):
                counts["failed"] += 1
                continue

            image = Image.objects.create(
                image=ContentFile(content, name=cached_item.get("filename") or f"{item_id}.jpg"),
                pin=pin,
                location=pin.location,
                profile=profile,
                source=ImageSource.GOOGLE_PHOTOS,
                checksum=checksum,
                file_size=len(content),
                source_url=media_item_web_url(item_id),
                # Same quarantine an ordinary upload gets: these are raw bytes from a
                # third-party API, stored unread, and visible the moment the row exists.
                # process_image_upload (enqueued below) scans, strips and clears it.
                pending_scan=True,
            )
        log_visit_on_pin(profile, image, pin)
        safely_enqueue_task(process_image_upload, image.pk)
        existing_checksums.add(checksum)
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
    from urbanlens.dashboard.services.admin.backups import scheduled_backup_due

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

    Fired hourly by Celery beat. ``services.locations.enrichment.run_enrichment_cycle``
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

    from urbanlens.dashboard.services.locations.enrichment import RUN_LOCK_CACHE_KEY, run_enrichment_cycle

    _lock_token = acquire_lock(RUN_LOCK_CACHE_KEY, 3300)
    if _lock_token is None:
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
        release_lock(RUN_LOCK_CACHE_KEY, _lock_token)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def refresh_pin_web_search(self, pin_id: int) -> int:
    """Pre-warm the shared web-search cache for a pin's Location."""
    from urllib.parse import urlparse

    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.search.search import format_search_date, search_web

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


# These safety check-in beat tasks share the RUN_LOCK_CACHE_KEY-style guard already
# used by run_scheduled_enrichment: they run every 5 minutes (see CELERY_BEAT_SCHEDULE), and
# without a lock, an overrunning execution (many due checkins, slow SMTP) racing the next
# scheduled tick could process the same rows twice - most seriously for escalation, which
# would otherwise re-email emergency contacts.
_CHECKIN_REMINDER_LOCK_CACHE_KEY = "urbanlens:safety:reminder-lock"
_CHECKIN_FINAL_WARNING_LOCK_CACHE_KEY = "urbanlens:safety:final-warning-lock"
_CHECKIN_ESCALATION_LOCK_CACHE_KEY = "urbanlens:safety:escalation-lock"
_CHECKIN_ARCHIVAL_SWEEP_LOCK_CACHE_KEY = "urbanlens:safety:archival-sweep-lock"
_CHECKIN_LOCK_TIMEOUT_SECONDS = 270  # just under the 5-minute beat interval


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_due_checkin_reminders() -> int:
    """Send the check-in-due reminder for every safety check-in whose time has arrived."""
    from django.core.cache import cache

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import send_checkin_reminder

    _lock_token = acquire_lock(_CHECKIN_REMINDER_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("send_due_checkin_reminders: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.due_for_reminder():
            # Isolated per check-in for the same reason the archival sweep below is:
            # this queryset has a deterministic ordering, so one repeatably-failing row
            # would otherwise abort the run at the same position on every tick and
            # silently starve every check-in behind it.
            try:
                send_checkin_reminder(checkin)
                count += 1
            except Exception:
                logger.exception("Safety checkin %s failed to send its due reminder; will retry next sweep", checkin.pk)
        if count:
            logger.info("Sent %s safety check-in reminder(s)", count)
        return count
    finally:
        release_lock(_CHECKIN_REMINDER_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_final_checkin_warnings() -> int:
    """Send a final "check in now" warning for every safety check-in about to escalate."""
    from django.core.cache import cache

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import send_final_warning

    _lock_token = acquire_lock(_CHECKIN_FINAL_WARNING_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("send_final_checkin_warnings: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.due_for_final_warning():
            try:
                send_final_warning(checkin)
                count += 1
            except Exception:
                logger.exception("Safety checkin %s failed to send its final warning; will retry next sweep", checkin.pk)
        if count:
            logger.info("Sent %s safety check-in final warning(s)", count)
        return count
    finally:
        release_lock(_CHECKIN_FINAL_WARNING_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def escalate_overdue_checkins() -> int:
    """Notify emergency contacts for every safety check-in whose grace period has elapsed."""
    from django.core.cache import cache

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import escalate_checkin

    _lock_token = acquire_lock(_CHECKIN_ESCALATION_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("escalate_overdue_checkins: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.overdue():
            # The most consequential of the three sweeps to isolate: this is the call
            # that reaches someone's emergency contacts, and escalate_checkin is already
            # per-contact idempotent, so retrying a failed one next tick only reaches the
            # contacts the failed attempt never got to.
            try:
                escalate_checkin(checkin)
                count += 1
            except Exception:
                logger.exception("Safety checkin %s failed to escalate to its emergency contacts; will retry next sweep", checkin.pk)
        if count:
            logger.info("Escalated %s overdue safety check-in(s)", count)
        return count
    finally:
        release_lock(_CHECKIN_ESCALATION_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def archive_safety_checkin(checkin_id: int) -> None:
    """Encrypt-and-scrub one resolved check-in, dispatched with a countdown= at resolution
    time (``services.visits.safety.schedule_checkin_archival``) for responsiveness.

    Idempotent - ``services.visits.safety.archive_checkin`` no-ops if the check-in already has
    an archive, so a duplicate dispatch (or this task racing the sweep below) is harmless.
    """
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import archive_checkin

    checkin = SafetyCheckin.objects.filter(pk=checkin_id).first()
    if checkin is not None:
        archive_checkin(checkin)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_due_safety_checkin_archival() -> int:
    """Backstop for ``archive_safety_checkin``'s countdown-scheduled dispatch.

    A broker/worker restart can drop a countdown-scheduled task outright; a bare
    5-minute poll alone would also make the "no other viewers - archive immediately"
    case visibly wait up to 5 minutes, which isn't "immediately". Running both gives
    responsiveness on the common path and durability against the scheduled task
    getting lost - the same trade-off the other checkin beat tasks above already make
    for their own timing precision vs. this file's 5-minute cadence.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import archive_checkin

    _lock_token = acquire_lock(_CHECKIN_ARCHIVAL_SWEEP_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("sweep_due_safety_checkin_archival: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.due_for_archival():
            # One checkin's failure (e.g. a malformed key bundle) must not stop the
            # sweep from archiving every other overdue checkin in this same run -
            # each is independent, and the next sweep will retry only the failed one.
            try:
                archive_checkin(checkin)
                count += 1
            except Exception:
                logger.exception("Safety checkin %s failed to archive during sweep; will retry next sweep", checkin.pk)
        if count:
            logger.info("Archived %s overdue safety check-in(s)", count)
        return count
    finally:
        release_lock(_CHECKIN_ARCHIVAL_SWEEP_LOCK_CACHE_KEY, _lock_token)


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

    Each row's restore payload is stored directly on the row itself (see
    ``models.undo.UndoAction``'s docstring), not in a cache, specifically so
    an entry's restorability depends only on its own ``created`` timestamp
    versus ``UNDO_RETENTION`` - not on a separately-expiring cache TTL. This
    task just deletes rows once that window has passed, so the settings
    page's history list doesn't need to filter expired rows forever.
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
    ``services.messaging.dm_location_detection``) - coordinates are detected inline at
    send time, but addresses need a geocoding API call, which never belongs
    in the request path.

    Args:
        message_id: PK of the just-sent message to scan.

    Returns:
        Number of new location mentions recorded.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.services.messaging.dm_location_detection import detect_address_mentions

    message = DirectMessage.objects.filter(pk=message_id).select_related("sender", "recipient").first()
    if message is None:
        return 0
    return len(detect_address_mentions(message))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def hard_delete_expired_direct_messages(batch_size: int = 2000, max_per_run: int = 50000) -> int:
    """Permanently delete every direct message past its sender's disappearing-message window.

    Unlike delete_message_for_everyone (a tombstone - the row and its content
    stay in the DB, just hidden from both parties' rendered view),
    DirectMessage.is_expired_for_recipient only ever gated *display*: the row
    and its body/ciphertext sat in the DB untouched forever. This sweep is
    what actually removes it. Image.direct_message is SET_NULL (not CASCADE),
    so attached images are explicitly deleted here too - otherwise they'd
    survive as orphaned, still-unencrypted files after the message is gone.

    Work is taken in batches rather than as one set. In steady state a single
    hourly run has one hour of expiries to clear and takes one batch, but a
    backlog becoming due at once - the first run after a retention-policy
    change, or after the beat worker was down - would otherwise pull every due
    id into memory and send it back as a single ``IN (...)`` list, against
    Postgres' parameter and planning limits.

    A file shared by rows in different batches survives the earlier batch (its
    other row still references it) and is removed by the later one, since the
    earlier batch's rows are gone by then.

    Args:
        batch_size: Rows to claim per iteration.
        max_per_run: Ceiling on one invocation, so a large backlog drains over
            several scheduled runs instead of running unboundedly long. Any
            remainder is picked up by the next run.

    Returns:
        Number of messages deleted.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.media.images import delete_stored_file

    deleted = 0
    while deleted < max_per_run:
        take = min(batch_size, max_per_run - deleted)
        due_ids = list(DirectMessage.objects.due_for_hard_delete().values_list("id", flat=True)[:take])
        if not due_ids:
            break

        expiring = list(Image.objects.filter(direct_message_id__in=due_ids))
        expiring_pks = [image.pk for image in expiring]
        for image in expiring:
            if image.image:
                try:
                    delete_stored_file(image, also_deleting=expiring_pks)
                except OSError:
                    logger.exception("Failed to delete image file %s for expiring direct message %s", image.pk, image.direct_message_id)
        Image.objects.filter(direct_message_id__in=due_ids).delete()

        DirectMessage.objects.filter(id__in=due_ids).delete()
        deleted += len(due_ids)
        if len(due_ids) < take:
            break

    if deleted:
        logger.info("Hard-deleted %s expired direct message(s)", deleted)
    return deleted


#: Overlap lock for the deletion-reminder sweep, matching the safety reminder
#: tasks above. It is the only hourly beat task whose repetition is *visible to
#: a user*: `due_for_deletion_reminder` filters on
#: `deletion_reminder_sent_at__isnull=True`, which guards at selection time,
#: while `send_deletion_reminder` emails first and marks afterwards - so two
#: overlapping runs both select the same profile and both send.
#:
#: A lock rather than the claim-before-side-effect fix used elsewhere in this
#: codebase, because the failure directions are not symmetric here: a duplicate
#: is a second "your account will be deleted tomorrow" notice, while a lost one
#: is *no* warning before a permanent deletion. A lock loses nothing - the
#: skipped run leaves the marker unset, so the next tick sends it.
_DELETION_REMINDER_LOCK_CACHE_KEY = "urbanlens:account:deletion-reminder-lock"
_DELETION_REMINDER_LOCK_TIMEOUT_SECONDS = 3300  # just under the hourly beat interval


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_account_deletion_reminders() -> int:
    """Send the "1 day left" reminder for every account approaching its hard delete."""
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.profile.account_deletion import send_deletion_reminder

    _lock_token = acquire_lock(_DELETION_REMINDER_LOCK_CACHE_KEY, _DELETION_REMINDER_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("send_account_deletion_reminders: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for profile in Profile.objects.due_for_deletion_reminder():
            send_deletion_reminder(profile)
            count += 1
        if count:
            logger.info("Sent %s account deletion reminder(s)", count)
        return count
    finally:
        release_lock(_DELETION_REMINDER_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def hard_delete_expired_accounts() -> int:
    """Permanently delete every account whose 7-day deletion grace period has elapsed."""
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile

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
def fetch_panel_source(source_key: str, pin_id: int, flight_token: str | None = None) -> None:
    """Fetch one external-data panel's upstream data in the background.

    Scheduled by ``external_data.schedule_panel_fetch`` when a Private Pin page
    finds a panel's store empty; the page polls until this task persists the
    result (LocationCache row, Boundary geometry column, or warmed slide caches).

    Args:
        source_key: An ``external_data.panel_sources()`` key.
        pin_id: PK of the pin whose panel data should be fetched.
        flight_token: Single-flight token from ``schedule_panel_fetch``; the
            fetch releases the marker only while it is still its own. Absent for
            tasks enqueued before tokens existed.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import run_panel_fetch

    pin = Pin.objects.select_related("location").filter(pk=pin_id).first()
    if pin is None:
        logger.info("fetch_panel_source: pin %s no longer exists", pin_id)
        return
    run_panel_fetch(source_key, pin, flight_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_direct_message_email_if_unread(message_id: int) -> None:
    """Send the delayed "new message" email, unless it's since been read or already sent.

    Scheduled by ``services.messaging.direct_messages._schedule_message_email`` with a
    countdown, giving a logged-in recipient a chance to read the message
    organically first. No-ops if the message was read in the meantime, or if
    an earlier message in the same unread streak already triggered this email
    (``services.messaging.direct_messages.send_message_email_now`` sets that marker).

    Args:
        message_id: PK of the message to check and possibly email about.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.services.messaging.direct_messages import can_direct_message, is_email_debounced, send_message_email_now

    try:
        message = DirectMessage.objects.select_related("sender", "recipient__user").get(pk=message_id)
    except DirectMessage.DoesNotExist:
        return
    if message.read_at is not None:
        return
    # "Still unread" is not "still there". Both delete-for-everyone and the
    # recipient's own delete are soft - the row survives with a timestamp, and
    # the app shows a tombstone - so without this the delayed alert delivers, out
    # of band and permanently, the text the app has already withdrawn. Asking the
    # same helper the UI asks keeps the two from drifting, and picks up expired
    # disappearing messages for free.
    if message.tombstone_text_for(message.recipient_id) is not None:
        return
    # Re-asked, not remembered: sending was permitted 120 seconds ago, and a
    # block is most often placed in exactly that window - right after the message
    # that prompted it. Asking the same helper create_direct_message asks keeps
    # the two from drifting, and covers a recipient who has since tightened their
    # direct-message visibility for the same reason.
    if not can_direct_message(message.sender, message.recipient):
        return
    if is_email_debounced(message.sender_id, message.recipient_id):
        return
    send_message_email_now(message)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_direct_message_text_alerts_if_unread(message_id: int) -> None:
    """Send the delayed WhatsApp/SMS "new message" alert, unless read or already alerted.

    Scheduled by ``services.messaging.direct_messages._schedule_message_text_alerts``
    with a countdown, mirroring the delayed-email flow: no-ops if the message
    was read in the meantime or an earlier message in the same unread streak
    already triggered an alert (``send_message_text_alerts_now`` sets that
    marker; viewing the conversation clears it).

    Args:
        message_id: PK of the message to check and possibly alert about.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.services.messaging.direct_messages import can_direct_message, is_text_alert_debounced, send_message_text_alerts_now

    try:
        message = DirectMessage.objects.select_related("sender", "recipient__user").get(pk=message_id)
    except DirectMessage.DoesNotExist:
        return
    if message.read_at is not None:
        return
    # "Still unread" is not "still there". Both delete-for-everyone and the
    # recipient's own delete are soft - the row survives with a timestamp, and
    # the app shows a tombstone - so without this the delayed alert delivers, out
    # of band and permanently, the text the app has already withdrawn. Asking the
    # same helper the UI asks keeps the two from drifting, and picks up expired
    # disappearing messages for free.
    if message.tombstone_text_for(message.recipient_id) is not None:
        return
    # Re-asked, not remembered: sending was permitted 120 seconds ago, and a
    # block is most often placed in exactly that window - right after the message
    # that prompted it. Asking the same helper create_direct_message asks keeps
    # the two from drifting, and covers a recipient who has since tightened their
    # direct-message visibility for the same reason.
    if not can_direct_message(message.sender, message.recipient):
        return
    if is_text_alert_debounced(message.sender_id, message.recipient_id):
        return
    send_message_text_alerts_now(message)


@shared_task
def prune_api_call_logs() -> int:
    """Delete ApiCallLog rows older than every consumer's longest window.

    The table is written on every external API call and, until this task
    existed, never trimmed - ``ApiCallLog.prune_older_than_days`` was
    documented as the way to trim it, but nothing ever called it, so the
    rate-limit COUNTs that run before each call scanned an ever-growing
    table. Scheduled daily (see ``CELERY_BEAT_SCHEDULE``).

    Retention is set by the longest reader, not the rate limiter: limits need
    30 days, but ``services.admin.cost_tracking.monthly_cost_series``
    reconstructs the public costs page's 12-month API-spend chart from these
    rows - pruning at the model helper's 90-day default would silently zero
    out three-quarters of that chart. 400 days covers 13 calendar months with
    margin.

    Returns:
        Number of rows deleted.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    deleted = ApiCallLog.prune_older_than_days(_API_CALL_LOG_RETENTION_DAYS)
    if deleted:
        logger.info("Pruned %d ApiCallLog row(s) older than %d days", deleted, _API_CALL_LOG_RETENTION_DAYS)
    return deleted


#: See prune_api_call_logs: 12 months of cost-series history plus margin.
_API_CALL_LOG_RETENTION_DAYS = 400


@shared_task
def prune_pin_tombstones() -> int:
    """Remove pin-deletion tombstones older than the sync retention window.

    Scheduled daily (see ``CELERY_BEAT_SCHEDULE``). Retention is
    ``services.pins.pin_sync.TOMBSTONE_RETENTION`` - the longest supported
    sync-client offline gap. A client whose ``deleted_since`` predates that
    floor gets an HTTP 410 full-resync signal from ``pins/deleted/`` instead
    of a silently incomplete deletions feed, so pruning can never cause a
    quiet miss.

    Returns:
        Number of tombstone rows deleted.
    """
    from urbanlens.dashboard.models.pin_tombstone import PinTombstone
    from urbanlens.dashboard.services.pins.pin_sync import TOMBSTONE_RETENTION

    deleted = PinTombstone.objects.prune_older_than(TOMBSTONE_RETENTION)
    if deleted:
        logger.info("Pruned %d pin tombstone(s) older than %s", deleted, TOMBSTONE_RETENTION)
    return deleted


@shared_task
def evaluate_public_pin_candidates() -> dict[str, int]:
    """Run the public-pin eligibility engine and settle open votes.

    Scheduled hourly (see ``CELERY_BEAT_SCHEDULE``). Everything lives in
    ``services.pins.public_pins`` - this is only the beat entry point. Idempotent
    at any frequency; hourly keeps vote outcomes and suggestion fan-out
    reasonably fresh without the engine's aggregate queries running hot.

    Returns:
        Transition counters (opened/reopened/suspended/passed/rejected).
    """
    from urbanlens.dashboard.services.pins import public_pins

    counters = public_pins.evaluate_public_pin_candidates()
    if any(counters.values()):
        logger.info("Public-pin evaluation: %s", counters)
    return counters


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_notification_text_alerts_if_unread(notification_id: int) -> None:
    """Send the delayed WhatsApp/SMS alert for a site notification, unless read or debounced.

    Scheduled by ``services.notifications.notification_text_alerts.schedule_notification_text_alerts``
    (via the ``notification_text_alerts`` post_save signal) with a countdown,
    mirroring the DM text-alert flow: no-ops when the notification was read in
    the meantime, when a same-type text recently went to this recipient, or
    when the recipient turned the toggles off after it was enqueued.

    Args:
        notification_id: PK of the notification to check and possibly alert about.
    """
    from urbanlens.dashboard.models.notifications.meta import Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.notifications.notification_text_alerts import is_text_alert_debounced, send_notification_text_alerts_now

    try:
        notification = NotificationLog.objects.select_related("profile__user").get(pk=notification_id)
    except NotificationLog.DoesNotExist:
        return
    if notification.profile_id is None or notification.status != Status.UNREAD:
        return
    if is_text_alert_debounced(notification.profile_id, notification.notification_type):
        return
    send_notification_text_alerts_now(notification)


@shared_task
def broadcast_channel_group_message(group: str, message: dict[str, Any]) -> None:
    """Deliver ``message`` to every channel in channel-layer group ``group``.

    Runs the actual ``async_to_sync(channel_layer.group_send)`` call here, on
    ``celery-worker``'s prefork pool, rather than inline in whatever gunicorn
    gevent greenlet handled the request that triggered it - see
    ``services.core.channel_broadcast`` and docs/PROBLEMS.md's gevent/asyncio entry
    for why calling into asyncio directly from a gevent-scheduled request can
    raise ``SynchronousOnlyOperation`` on a *different*, unrelated concurrent
    request. Best-effort: a channel-layer failure is logged, not raised,
    matching every caller's existing "already durably saved, live delivery is
    a bonus" contract.

    Args:
        group: Channel-layer group name to deliver to.
        message: JSON-serializable event dict (must include a "type" key).
    """
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, message)
    except Exception:
        logger.exception("Failed to broadcast to channel-layer group %s", group)


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


@shared_task
def classify_trivia_submission(question_id: int) -> None:
    """Classify one pending user-submitted Trivia question and record its verdict.

    No Celery autoretry: each attempt consumes an AI call, and this is a
    background action with no user waiting on it - if this task never runs
    (or the classifier can't reach AI right now), the question simply stays
    PENDING_REVIEW (silently excluded from rotation, see
    services.trivia.submission.classify_and_update), no different from any
    other transient Celery outage.

    Args:
        question_id: PK of the pending TriviaQuestion row.
    """
    from urbanlens.dashboard.models.trivia.model import TriviaQuestion
    from urbanlens.dashboard.services.trivia.submission import classify_and_update

    question = TriviaQuestion.objects.filter(pk=question_id).select_related("location", "submitted_by").first()
    if question is None:
        logger.info("classify_trivia_submission: question %s no longer exists", question_id)
        return
    classify_and_update(question)


@shared_task
def run_scheduled_trivia_generation() -> dict:
    """Generate AI trivia questions for a bounded batch of not-yet-processed wikis.

    Fired hourly by Celery beat, mirroring run_scheduled_enrichment's
    single-flight lock (a run that's still going when the next hour ticks
    over is left alone rather than started twice). No autoretry: each
    wiki considered spends AI tokens on generation and classification, so an
    automatic retry would silently multiply cost; a skipped wiki is simply
    picked up on the next scheduled run.

    Returns:
        The sweep summary dict, or a skip marker when another run holds the
        single-flight lock.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.trivia.generation import sweep_wikis_for_generation

    lock_key = "trivia_generation_sweep_lock"
    _lock_token = acquire_lock(lock_key, 3300)
    if _lock_token is None:
        logger.info("run_scheduled_trivia_generation: another sweep is still running; skipping")
        return {"skipped": "already_running"}
    try:
        return sweep_wikis_for_generation()
    finally:
        release_lock(lock_key, _lock_token)


@shared_task
def run_scheduled_trivia_wiki_incorporation() -> dict:
    """Fold well-upvoted user-submitted Trivia questions into their location wikis.

    Fired hourly by Celery beat, mirroring run_scheduled_trivia_generation's
    single-flight lock (a run still going when the next hour ticks over is
    left alone rather than started twice). No autoretry: each candidate
    question considered spends AI tokens on writing and safety review, so an
    automatic retry would silently multiply cost; a skipped question is
    simply picked up on the next scheduled run.

    Returns:
        The sweep summary dict, or a skip marker when another run holds the
        single-flight lock.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.trivia.wiki_incorporation import sweep_questions_for_wiki_incorporation

    lock_key = "trivia_wiki_incorporation_sweep_lock"
    _lock_token = acquire_lock(lock_key, 3300)
    if _lock_token is None:
        logger.info("run_scheduled_trivia_wiki_incorporation: another sweep is still running; skipping")
        return {"skipped": "already_running"}
    try:
        return sweep_questions_for_wiki_incorporation()
    finally:
        release_lock(lock_key, _lock_token)


#: Slugs a pin gets when it is created before anything knows what it is. Listed
#: explicitly so the sweep below is an indexed lookup rather than a scan of every
#: pin; ``Pin.slug_is_placeholder`` still has the final say on each candidate.
_PLACEHOLDER_SLUGS = ("unnamed-location", "unnamed", "dropped-pin", "pin", "location", "place", "point", "marker", "unknown-location", "unknown")

#: How old a pin must be before this sweep will change its slug.
#:
#: Generous on purpose. The window that matters is "somebody has this pin's
#: detail page open", and the cost of waiting is that a legacy pin keeps a
#: placeholder URL an hour longer - which it has already kept for months.
_RESLUG_MIN_AGE = timedelta(hours=1)


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

    # Slugs are generated once and never revisited, so a pin created before
    # anything knew what it was keeps `unnamed-location` in its URL even after it
    # is named - reported from staging on a pin called "HRSH" with three aliases.
    # Refreshed here rather than on save: the pins that need it were named long
    # ago, and only a slug that still reads as a placeholder is replaced.
    #
    # "So no working link changes" was the original claim here, and it is not
    # quite true: a link can be *open*. A pin created minutes ago has its detail
    # page rendered with the old slug baked into every HTMX panel URL, so
    # replacing the slug underneath it 404s those panels, and the global
    # `htmx:responseError` handler turns each into an error toast on a pin the
    # user has just made. `tests/integration/` caught exactly that; see
    # docs/PROBLEMS.md, 2026-08-23. The age guard restores the assumption by
    # making it true - this sweep is for legacy data, per the docstring above,
    # and legacy data is not five minutes old.
    reslugged = 0
    for pin in Pin.objects.filter(slug__in=_PLACEHOLDER_SLUGS, created__lt=timezone.now() - _RESLUG_MIN_AGE).select_related("location")[:batch_size]:
        if pin.refresh_placeholder_slug():
            reslugged += 1
    if reslugged:
        logger.info("upgrade_placeholder_pin_names: replaced %s placeholder slug(s)", reslugged)

    return upgraded


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def dispatch_native_push(notification_id: int) -> int:
    """Deliver one notification to the recipient's registered native push devices.

    Enqueued by ``models.notifications.signals.enqueue_native_push`` on every
    ``NotificationLog`` insert; exits immediately for the (common) profile with
    no registered devices. Delivery itself is best-effort per device - see
    ``services.notifications.push.send_push_to_profile``.

    Args:
        notification_id: Primary key of the ``NotificationLog`` row to deliver.

    Returns:
        Number of devices successfully delivered to.
    """
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.notifications.signals import as_push_payload
    from urbanlens.dashboard.services.notifications.push import send_push_to_profile

    notification = NotificationLog.objects.filter(pk=notification_id).first()
    if notification is None or not notification.profile_id:
        return 0
    return send_push_to_profile(notification.profile_id, as_push_payload(notification))


_SPOTGUESSR_STALL_SWEEP_LOCK_CACHE_KEY = "urbanlens:spotguessr:stall-sweep-lock"
_SPOTGUESSR_STALL_SWEEP_LOCK_TIMEOUT_SECONDS = 110  # just under the 2-minute beat interval


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_stalled_spotguessr_sessions() -> int:
    """Force-reveal any SpotGuessr round that's been open too long.

    The safety net for a multiplayer round that can otherwise stall forever:
    a round only completes once every joined participant has guessed
    (``services.spotguessr.session.submit_guess``), but a participant who
    simply closes their tab is invisible to that check - there's no
    disconnect signal wired into the game state (see the SpotGuessr audit's
    "multiplayer stall" finding). This sweep finds any session whose current
    round has sat unrevealed past ``STALL_ROUND_TIMEOUT_MINUTES`` and force-
    reveals it (``force_reveal_round``), which either lets the game continue
    with whoever did guess, or marks the session ``ABANDONED`` if literally
    nobody did.
    """
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone

    from urbanlens.dashboard.models.spotguessr.model import GameSession
    from urbanlens.dashboard.services.spotguessr.session import STALL_ROUND_TIMEOUT_MINUTES, force_reveal_round

    _lock_token = acquire_lock(_SPOTGUESSR_STALL_SWEEP_LOCK_CACHE_KEY, _SPOTGUESSR_STALL_SWEEP_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("sweep_stalled_spotguessr_sessions: a previous run is still in flight; skipping")
        return 0
    try:
        cutoff = timezone.now() - timedelta(minutes=STALL_ROUND_TIMEOUT_MINUTES)
        count = 0
        for session in GameSession.objects.stalled(cutoff=cutoff):
            current_round = session.rounds.filter(revealed_at__isnull=True).first()
            if current_round is None:
                continue  # raced with a normal guess completing it - nothing to do
            try:
                force_reveal_round(current_round)
            except Exception:
                logger.exception("Failed to force-reveal stalled SpotGuessr round %s", current_round.pk)
                continue
            count += 1
        if count:
            logger.info("Force-revealed %s stalled SpotGuessr round(s)", count)
        return count
    finally:
        release_lock(_SPOTGUESSR_STALL_SWEEP_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def prewarm_spotguessr_round(session_id: int, sequence_index: int) -> bool:
    """Pre-select a SpotGuessr session's next round, so it's ready the instant a player reaches it.

    Queued by ``services.spotguessr.session.get_or_create_round`` right after
    it creates the round *before* this one - by the time that round is
    guessed and revealed, this round's location (and, for Street View mode,
    its Google Maps imagery - see ``services.spotguessr.street_view``, whose
    result this warms via the same lat/lng cache key) is already picked and
    cached, so the round that actually gets created next is a cache hit
    instead of live selection (see ``services.spotguessr.prewarm``). A no-op
    if the session has since ended, this round already exists (a page reload
    or another guess raced this task to it), or nothing eligible is left -
    none of those are errors, just nothing worth prewarming anymore.

    Args:
        session_id: The session to prewarm a round for.
        sequence_index: The round's 0-based position within the session.

    Returns:
        True if a round was prewarmed, False if there was nothing to do.
    """
    from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, GameSessionStatus
    from urbanlens.dashboard.services.spotguessr import prewarm
    from urbanlens.dashboard.services.spotguessr.session import config_from_session, generate_round_content

    try:
        session = GameSession.objects.get(pk=session_id)
    except GameSession.DoesNotExist:
        return False
    if session.status != GameSessionStatus.ACTIVE:
        return False

    existing_rounds = list(GameRound.objects.for_session(session).select_related("location"))
    if any(round_.sequence_index == sequence_index for round_ in existing_rounds):
        return False  # already created - a reload or another guess beat this task to it

    joined_participants = list(session.participants.joined().select_related("profile"))
    if not joined_participants:
        return False
    participants = [participant.profile for participant in joined_participants]
    excluded_ids = [round_.location_id for round_ in existing_rounds]
    previous_location = existing_rounds[-1].location if existing_rounds else None

    config = config_from_session(session)
    picked = generate_round_content(session.mode, config, participants, excluded_ids, previous_location)
    if picked is None:
        return False
    location, content = picked
    prewarm.store_for_session(session.pk, sequence_index, location, content)
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def prewarm_spotguessr_solo_start(profile_id: int, mode: str, config_dict: dict) -> bool:
    """Pre-select a solo player's likely first round before they've even clicked "start".

    Queued from ``controllers.spotguessr.SpotGuessrHomeView`` on every visit
    to the SpotGuessr overview page, using the player's last-used settings
    (``SpotGuessrPreference.last_config``) and most-recently-played mode as
    the best guess of what they'll start next. Keyed by a fingerprint of the
    exact config (see ``services.spotguessr.prewarm``), so it's simply never
    redeemed - not wrongly redeemed - if the player changes a setting before
    actually starting.

    Args:
        profile_id: The player who loaded the SpotGuessr overview page.
        mode: The guessed ``SpotGuessrMode`` they'll start.
        config_dict: A ``GameConfig.to_dict()`` snapshot of their guessed
            settings (unknown keys ignored, mirroring
            ``session.config_from_session``).

    Returns:
        True if a round was prewarmed, False if there was nothing eligible.
    """
    import dataclasses

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.spotguessr import eligibility, prewarm
    from urbanlens.dashboard.services.spotguessr.session import GameConfig, generate_round_content

    try:
        profile = Profile.objects.get(pk=profile_id)
    except Profile.DoesNotExist:
        return False

    known_fields = {f.name for f in dataclasses.fields(GameConfig)}
    config = GameConfig(**{key: value for key, value in config_dict.items() if key in known_fields})
    if not eligibility.has_eligible_locations([profile], require_visited_by_all=config.require_visited_all, geo_bounds=config.geo_bounds):
        return False

    picked = generate_round_content(mode, config, [profile], [], None)
    if picked is None:
        return False
    location, content = picked
    prewarm.store_for_solo_start(profile_id, mode, config, location, content)
    return True


_TRIVIA_STALL_SWEEP_LOCK_CACHE_KEY = "urbanlens:trivia:stall-sweep-lock"
_TRIVIA_STALL_SWEEP_LOCK_TIMEOUT_SECONDS = 110  # just under the 2-minute beat interval


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_stalled_trivia_sessions() -> int:
    """Force-reveal any Trivia round that's been open too long.

    The safety net for a multiplayer round that can otherwise stall forever:
    a round only completes once every joined participant has answered
    (``services.trivia.session.submit_answer``), but a participant who
    simply closes their tab is invisible to that check - there's no
    disconnect signal wired into the game state. Mirrors
    ``sweep_stalled_spotguessr_sessions`` exactly. This sweep finds any
    session whose current round has sat unrevealed past
    ``STALL_ROUND_TIMEOUT_MINUTES`` and force-reveals it
    (``force_reveal_round``), which either lets the game continue with
    whoever did answer, or marks the session ``ABANDONED`` if literally
    nobody did.
    """
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone

    from urbanlens.dashboard.models.trivia.model import TriviaSession
    from urbanlens.dashboard.services.trivia.session import STALL_ROUND_TIMEOUT_MINUTES, force_reveal_round

    _lock_token = acquire_lock(_TRIVIA_STALL_SWEEP_LOCK_CACHE_KEY, _TRIVIA_STALL_SWEEP_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("sweep_stalled_trivia_sessions: a previous run is still in flight; skipping")
        return 0
    try:
        cutoff = timezone.now() - timedelta(minutes=STALL_ROUND_TIMEOUT_MINUTES)
        count = 0
        for session in TriviaSession.objects.stalled(cutoff=cutoff):
            current_round = session.rounds.filter(revealed_at__isnull=True).first()
            if current_round is None:
                continue  # raced with a normal answer completing it - nothing to do
            try:
                force_reveal_round(current_round)
            except Exception:
                logger.exception("Failed to force-reveal stalled Trivia round %s", current_round.pk)
                continue
            count += 1
        if count:
            logger.info("Force-revealed %s stalled Trivia round(s)", count)
        return count
    finally:
        release_lock(_TRIVIA_STALL_SWEEP_LOCK_CACHE_KEY, _lock_token)


_CONSENSUS_STALL_SWEEP_LOCK_CACHE_KEY = "urbanlens:consensus:stall-sweep-lock"
_CONSENSUS_STALL_SWEEP_LOCK_TIMEOUT_SECONDS = 110  # just under the 2-minute beat interval


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_stalled_consensus_sessions() -> int:
    """Force-resolve any Consensus round that's been open too long.

    Unlike SpotGuessr/Trivia, a Consensus round has *two* sub-phases that
    can each stall independently: answer-collection (mirrors
    ``sweep_stalled_spotguessr_sessions`` - force-reveals via
    ``force_reveal_round``) and, for a competitive round whose answers
    disagreed, the follow-on vote (force-tallies via
    ``force_resolve_vote``). Both are swept in the same task run.
    """
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone

    from urbanlens.dashboard.models.consensus.model import ConsensusRoundResolution, ConsensusSession
    from urbanlens.dashboard.services.consensus.session import STALL_ROUND_TIMEOUT_MINUTES, force_resolve_vote, force_reveal_round

    _lock_token = acquire_lock(_CONSENSUS_STALL_SWEEP_LOCK_CACHE_KEY, _CONSENSUS_STALL_SWEEP_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("sweep_stalled_consensus_sessions: a previous run is still in flight; skipping")
        return 0
    try:
        cutoff = timezone.now() - timedelta(minutes=STALL_ROUND_TIMEOUT_MINUTES)
        count = 0
        for session in ConsensusSession.objects.answer_stalled(cutoff=cutoff):
            current_round = session.rounds.filter(resolution=ConsensusRoundResolution.PENDING).first()
            if current_round is None:
                continue  # raced with a normal answer completing it - nothing to do
            try:
                force_reveal_round(current_round)
            except Exception:
                logger.exception("Failed to force-reveal stalled Consensus round %s", current_round.pk)
                continue
            count += 1
        for session in ConsensusSession.objects.vote_stalled(cutoff=cutoff):
            current_round = session.rounds.filter(resolution=ConsensusRoundResolution.VOTE_OPEN).first()
            if current_round is None:
                continue  # raced with a normal vote completing it - nothing to do
            try:
                force_resolve_vote(current_round)
            except Exception:
                logger.exception("Failed to force-resolve stalled Consensus vote for round %s", current_round.pk)
                continue
            count += 1
        if count:
            logger.info("Force-resolved %s stalled Consensus round(s)", count)
        return count
    finally:
        release_lock(_CONSENSUS_STALL_SWEEP_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def recompute_fact_confidence(fact_id: int) -> None:
    """Recompute one Fact's confidence/status/value from its accumulated evidence.

    Queued (never called inline) from every Facts evidence write site - see
    ``services.facts.evidence.record_evidence``. Per-fact evidence volume is
    small by construction, mirroring the same reasoning behind SpotGuessr's
    synchronous-but-cheap ``recompute_estimated_coordinates``, so no
    debounce/locking is needed here.
    """
    from urbanlens.dashboard.services.facts.confidence import recompute

    recompute(fact_id)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def process_device_scan_upload(self, upload_id: int) -> bool:
    """Classify, wiki-match, and cluster one wireless device-scan upload.

    Runs on the default queue - real CPU-bound geometry work, not
    ``panel_fetch`` (same reasoning as ``classify_detail_marker``). Always
    marks the upload PROCESSED or FAILED by the time this returns, even on an
    unexpected error, so a stuck PENDING row always means the task never ran
    at all rather than having failed silently mid-way.

    Claims the upload by flipping PENDING -> PROCESSED atomically before doing
    any work, so a redelivered or manually retried task for an upload that
    already finished (or is being worked by another worker) is a no-op rather
    than re-running ``record_absence_report`` and inflating a marker's absence
    streak a second time for the same physical report.

    Args:
        upload_id: PK of the DeviceScanUpload to process.

    Returns:
        True when this call claimed and processed the upload (successfully or
        not); False when it no longer exists, or was already claimed by a
        prior run.
    """
    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload, ScanUploadStatus
    from urbanlens.dashboard.services.device_scan.pipeline import process_scan_upload

    claimed = DeviceScanUpload.objects.filter(pk=upload_id, status=ScanUploadStatus.PENDING).update(status=ScanUploadStatus.PROCESSED)
    if not claimed:
        logger.info("process_device_scan_upload: upload %s no longer exists or is not pending", upload_id)
        return False

    upload = DeviceScanUpload.objects.select_related("profile").prefetch_related("entries__device", "entries__expected_marker").filter(pk=upload_id).first()
    if upload is None:
        return False

    update_task_progress(self, current=0, total=1, message="Processing device scan...")
    try:
        process_scan_upload(upload)
    except Exception as exc:
        logger.exception("process_device_scan_upload: failed for upload %s", upload_id)
        DeviceScanUpload.objects.filter(pk=upload_id).update(status=ScanUploadStatus.FAILED, error=str(exc))
        update_task_progress(self, current=1, total=1, message="Device scan processing failed")
        return True

    update_task_progress(self, current=1, total=1, message="Device scan processed")
    return True


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def evaluate_achievements_for_profile(profile_id: int, metric_keys: list[str] | None = None) -> int:
    """Grant any achievements a profile now qualifies for.

    Queued by ``models.achievements.signals`` after a contribution, but only
    when some active award actually measures the affected metric - so this runs
    rarely, and when it does it re-checks a single count rather than sweeping.

    Streak days are recorded synchronously by the signal, not here, so a task
    delayed past midnight cannot credit the wrong day.

    Args:
        profile_id: PK of the profile that contributed.
        metric_keys: Registry keys of the metrics to re-check. None re-checks
            every active achievement; an empty list re-checks nothing.

    Returns:
        How many awards were newly granted.
    """
    from urbanlens.dashboard.models.profile import Profile
    from urbanlens.dashboard.services.achievements.evaluate import evaluate_profile

    if metric_keys is not None and not metric_keys:
        return 0

    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        logger.info("evaluate_achievements_for_profile: profile %s no longer exists", profile_id)
        return 0

    return len(evaluate_profile(profile, metric_keys=metric_keys))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def backfill_achievement(achievement_id: int) -> int:
    """Grant a newly defined achievement to everyone who already qualifies.

    Queued when an admin saves an ``Achievement``, so awards added at any point
    reach users retroactively instead of only rewarding activity from then on.

    Args:
        achievement_id: PK of the achievement to backfill.

    Returns:
        How many profiles received the award.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement
    from urbanlens.dashboard.services.achievements.evaluate import evaluate_achievement_for_all

    achievement = Achievement.objects.filter(pk=achievement_id).first()
    if achievement is None:
        logger.info("backfill_achievement: achievement %s no longer exists", achievement_id)
        return 0

    return evaluate_achievement_for_all(achievement)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def score_reputation_event(event_id: int) -> str:
    """Work out what one recorded contribution was worth.

    Queued from ``models.reputation.signals`` after the row is already written.
    Deferred because establishing how badly a target needed a contribution
    means querying that target's state, which for photos can mean walking
    external gallery panels - by far the most expensive input in the model, and
    exactly the cost this feature must not add to a page load.

    Args:
        event_id: PK of the ledger row to value.

    Returns:
        The stored value as a string, or a short status when nothing was.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent
    from urbanlens.dashboard.services.reputation.scoring import recompute_total, score_event

    event = ReputationEvent.objects.filter(pk=event_id).first()
    if event is None:
        logger.info("score_reputation_event: event %s no longer exists", event_id)
        return "missing"
    if event.value is not None:
        # Already scored. acks_late means this task can be redelivered.
        return "already_scored"

    value = score_event(event)
    recompute_total(event.profile_id)
    return "unscorable" if value is None else str(value)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def recompute_reputation_total(profile_id: int) -> str:
    """Rebuild one profile's cached reputation totals from the ledger.

    Args:
        profile_id: Whose totals to rebuild.

    Returns:
        The new total as a string.
    """
    from urbanlens.dashboard.services.reputation.scoring import recompute_total

    return str(recompute_total(profile_id))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_reputation(chunk_size: int = 500) -> int:
    """Drain unscored ledger rows and rebuild any totals known to be stale.

    The backstop for a lost enqueue. ``safely_enqueue_task`` swallows broker
    failures and returns None, so a row can sit unscored indefinitely with
    nothing to notice - which is survivable only because the row itself was
    written synchronously and is therefore still there to find.

    Dispatch only: rows are sliced into bounded ranges, in pk order, and each
    range is handled by its own subtask, so a chunk that crashes costs its own
    range rather than the whole sweep.

    Args:
        chunk_size: Maximum rows per subtask.

    Returns:
        How many subtasks were dispatched.
    """
    from urbanlens.dashboard.models.reputation.model import ProfileReputation, ReputationEvent
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    chunk_size = max(1, chunk_size)
    pks = list(ReputationEvent.objects.unscored().order_by("pk").values_list("pk", flat=True))

    dispatched = 0
    for start in range(0, len(pks), chunk_size):
        chunk = pks[start : start + chunk_size]
        if safely_enqueue_task(sweep_reputation_range, chunk[0], chunk[-1]) is not None:
            dispatched += 1

    for profile_id in ProfileReputation.objects.stale().values_list("profile_id", flat=True):
        if safely_enqueue_task(recompute_reputation_total, profile_id) is not None:
            dispatched += 1

    return dispatched


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_reputation_range(start_pk: int, end_pk: int) -> int:
    """Score every unscored ledger row with ``start_pk <= pk <= end_pk``.

    Args:
        start_pk: First row in the range, inclusive.
        end_pk: Last row in the range, inclusive.

    Returns:
        How many rows were scored.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent
    from urbanlens.dashboard.services.reputation.scoring import recompute_total, score_event

    rows = ReputationEvent.objects.unscored().filter(pk__gte=start_pk, pk__lte=end_pk)
    touched: set[int] = set()
    scored = 0
    for event in rows:
        if score_event(event) is not None:
            scored += 1
        touched.add(event.profile_id)

    for profile_id in touched:
        recompute_total(profile_id)
    return scored


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_achievements(chunk_size: int = 1000) -> int:
    """Fan the nightly achievement sweep out as bounded profile-range subtasks.

    The nightly safety net. Some thresholds are crossed with no write to react
    to - "trips attended" ticks up simply because a trip's end date passed - and
    an enqueue is lost whenever the broker is briefly unreachable.

    This task only dispatches: profile pks are sliced, in pk order, into
    ranges of at most ``chunk_size`` and each range is evaluated by its own
    :func:`sweep_achievements_range` task. Evaluating everything in one task
    would hit the hard ``CELERY_TASK_TIME_LIMIT`` at scale and die
    mid-iteration; a bounded chunk cannot approach the limit, and a chunk that
    crashes anyway costs only its own range until the next nightly dispatch.
    Profiles created after dispatch are simply picked up the following night.

    Args:
        chunk_size: Maximum profiles per subtask.

    Returns:
        How many range subtasks were enqueued.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement
    from urbanlens.dashboard.models.profile import Profile
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    # Same gate the contribution signals apply: with no active award defined
    # there is provably nothing to evaluate, so don't fan out empty subtasks.
    if not Achievement.objects.active().exists():
        return 0

    chunk_size = max(1, chunk_size)
    pks = list(Profile.objects.order_by("pk").values_list("pk", flat=True))

    dispatched = 0
    for start in range(0, len(pks), chunk_size):
        chunk = pks[start : start + chunk_size]
        if safely_enqueue_task(sweep_achievements_range, chunk[0], chunk[-1]) is not None:
            dispatched += 1
    return dispatched


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sweep_achievements_range(start_pk: int, end_pk: int) -> int:
    """Evaluate every achievement for profiles with ``start_pk <= pk <= end_pk``.

    One chunk of the sweep dispatched by :func:`sweep_achievements`. The range
    is evaluated with one bulk metric pass for the whole chunk, so it costs on
    the order of the metric count in queries rather than ~30 per profile.

    Args:
        start_pk: Lowest profile pk in the chunk, inclusive.
        end_pk: Highest profile pk in the chunk, inclusive.

    Returns:
        Total awards granted across the chunk.
    """
    from urbanlens.dashboard.services.achievements.evaluate import evaluate_profiles_in_range

    return evaluate_profiles_in_range(start_pk, end_pk)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sync_stripe_subscriptions() -> int:
    """Re-sync every non-canceled RoleSubscription's status/price/threshold from Stripe.

    Webhooks (see controllers.billing_webhooks.StripeWebhookView) are the primary
    mechanism for keeping RoleSubscription in sync - this is the nightly safety net for
    deliveries Stripe couldn't complete (e.g. this server briefly unreachable exhausting
    Stripe's own retry schedule). Pure drift correction, not load-bearing for the core
    "did this charge clear the threshold" mechanic, which happens at webhook time.

    Returns:
        How many subscriptions were checked.
    """
    import stripe

    from urbanlens.dashboard.models.billing import RoleSubscription
    from urbanlens.dashboard.services.billing import stripe_client
    from urbanlens.dashboard.services.billing.webhooks import sync_from_stripe_subscription

    if not stripe_client.is_configured():
        return 0

    count = 0
    for role_subscription in RoleSubscription.objects.not_canceled().select_related("role"):
        try:
            stripe_subscription = stripe.Subscription.retrieve(role_subscription.stripe_subscription_id).to_dict()
        except stripe.StripeError:
            logger.exception("sync_stripe_subscriptions: failed to retrieve %s", role_subscription.stripe_subscription_id)
            continue
        # Applying the payload is inside the guard too, not just fetching it:
        # sync_from_stripe_subscription indexes into items.data[0], so one subscription
        # in an unexpected shape would otherwise abort the sweep for everyone after it.
        try:
            sync_from_stripe_subscription(role_subscription, stripe_subscription)
        except Exception:
            logger.exception("sync_stripe_subscriptions: failed to apply %s", role_subscription.stripe_subscription_id)
            continue
        count += 1
    return count


@shared_task
def advance_pwyw_usage_ledgers() -> int:
    """Advance every pay-what-you-want RoleSubscription's usage ledger.

    invoice.payment_succeeded already ticks a subscription's ledger the moment a
    payment lands (see services.billing.banking), but that's the only trigger while a
    subscription is actively billed - a canceled subscription gets no further Stripe
    events at all, so this daily sweep is what keeps its banked balance counting down
    (and eventually running out) once the money stops coming in.

    Returns:
        How many pay-what-you-want subscriptions were checked.
    """
    from urbanlens.dashboard.models.billing import RoleSubscription
    from urbanlens.dashboard.services.billing import banking

    count = 0
    for role_subscription in RoleSubscription.objects.filter(role__pay_what_you_want=True).select_related("role"):
        # This daily sweep is the only thing counting a canceled subscription's banked
        # balance down, so one row failing must not freeze every other user's ledger.
        try:
            banking.advance_usage_ledger(role_subscription)
            count += 1
        except Exception:
            logger.exception("advance_pwyw_usage_ledgers: failed to advance subscription %s", role_subscription.pk)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def cache_media_item_into_album(album_id: int, profile_id: int, source: str, url: str, page_url: str = "", caption: str = "") -> int | None:
    """Download an external media item and file the local copy into an album.

    The relevance vote is written synchronously by the request that queues
    this (it's a cheap DB write, and it's the part that must not be lost), so
    this task only owns the slow half: the HTTP download. Splitting it that
    way means a broker outage or a dead provider costs the user their photo,
    not their vote.

    Args:
        album_id: PK of the Album to file the photo into.
        profile_id: PK of the Profile the download is attributed to.
        source: Provider panel key (e.g. ``"wikimedia"``).
        url: The item's full-resolution image url.
        page_url: Optional provider page url, for attribution.
        caption: Optional caption carried from the gallery tile.

    Returns:
        PK of the materialized Image, or None if the album/profile vanished or
        the download failed.
    """
    from urbanlens.dashboard.models.album.model import Album
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item
    from urbanlens.dashboard.services.photos.albums import add_images_to_album, album_owner
    from urbanlens.dashboard.services.photos.redata_relevance import queue_relevance_vote

    album = Album.objects.filter(pk=album_id).select_related("parent_pin", "parent_wiki").first()
    profile = Profile.objects.filter(pk=profile_id).first()
    if album is None or profile is None:
        logger.info("cache_media_item_into_album: album %s or profile %s no longer exists", album_id, profile_id)
        return None

    owner = album_owner(album)
    # A personal (Profile-owned) album has no Pin/Wiki to attach media to at
    # all - checked directly rather than via `getattr(owner, "location", None)`,
    # which happened to also catch this case today only because Profile has no
    # `location` attribute of its own to shadow the default.
    if not isinstance(owner, Pin | Wiki):
        logger.info("cache_media_item_into_album: album %s has no pin or wiki to attach media to", album_id)
        return None
    location = owner.location
    if location is None:
        logger.info("cache_media_item_into_album: album %s has no location to attach media to", album_id)
        return None

    # isinstance rather than `album.parent_pin_id is not None`: it asks the
    # question directly of the object album_owner actually returned, so the two
    # cannot disagree - and narrows each argument to exactly the type
    # materialize_media_item expects, rather than assuming "not a Pin" means
    # "must be a Wiki" (album_owner can also return a bare Profile).
    try:
        image = materialize_media_item(
            location=location,
            profile=profile,
            source=source,
            url=url,
            page_url=page_url,
            caption=caption,
            pin=owner if isinstance(owner, Pin) else None,
            wiki=owner if isinstance(owner, Wiki) else None,
        )
    except MaterializeError:
        # The vote is already recorded and stays; only the download is lost.
        logger.warning("cache_media_item_into_album: failed to materialize %s for album %s", url, album_id)
        return None

    add_images_to_album(album, [image], profile)
    queue_relevance_vote(image, profile, is_relevant=True)
    return image.pk


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def cache_media_item_into_wiki(wiki_id: int, profile_id: int, source: str, url: str, page_url: str = "", caption: str = "") -> int | None:
    """Download an external media item and attach the local copy to a wiki.

    The wiki-send counterpart to :func:`cache_media_item_into_album`, and split the
    same way: the request validates and enqueues, this owns the slow half. Sending a
    full gallery selection meant up to 20 remote downloads inside one request, which
    is both a multi-second hang with no progress indicator and a request that can time
    out partway, leaving some photos attached and the rest silently dropped.

    Tolerates the wiki or profile being deleted between enqueue and run - by then the
    work is simply moot, which is not an error worth retrying.

    Args:
        wiki_id: PK of the Wiki to attach the photo to.
        profile_id: PK of the Profile the download is attributed to.
        source: Provider panel key (e.g. ``"wikimedia"``).
        url: The item's full-resolution image url.
        page_url: Optional provider page url, for attribution.
        caption: Optional caption carried from the gallery tile.

    Returns:
        PK of the materialized Image, or None if the wiki/profile vanished or the
        download failed.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item

    wiki = Wiki.objects.filter(pk=wiki_id).select_related("location").first()
    profile = Profile.objects.filter(pk=profile_id).first()
    if wiki is None or profile is None:
        logger.info("cache_media_item_into_wiki: wiki %s or profile %s no longer exists", wiki_id, profile_id)
        return None
    if wiki.location is None:
        logger.info("cache_media_item_into_wiki: wiki %s has no location to attach media to", wiki_id)
        return None

    try:
        image = materialize_media_item(
            location=wiki.location,
            profile=profile,
            source=source,
            url=url,
            page_url=page_url,
            caption=caption,
            wiki=wiki,
        )
    except MaterializeError:
        logger.warning("cache_media_item_into_wiki: failed to materialize %s for wiki %s", url, wiki_id)
        return None
    return image.pk


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def fetch_recorded_weather(location_id: int, iso_days: list[str]) -> int:
    """Fill a Location's recorded-weather cache for a set of days.

    Queued by the visit-history panel, which reads the cache without fetching:
    that panel renders a page of visits inline, and a page render must not
    block on an outbound call - a slow REData would hold up the whole visit
    list for a decorative line of text. So the first view shows what is known
    and asks for the rest, and the next view has it. This is the same
    fetch-behind/render-from-cache split every pin-detail panel already uses;
    it is only unusual here because the days come from the visits rather than
    from the location.

    Args:
        location_id: PK of the Location the days belong to.
        iso_days: ISO dates to fetch, as the caller found them missing.

    Returns:
        How many days ended up in the cache, for the task log.
    """
    from datetime import date

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.visit_weather import recorded_days

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("fetch_recorded_weather: location %s no longer exists", location_id)
        return 0

    days = []
    for iso in iso_days:
        try:
            days.append(date.fromisoformat(iso))
        except ValueError:
            logger.warning("fetch_recorded_weather: ignoring malformed date %r for location %s", iso, location_id)
    if not days:
        return 0
    return len(recorded_days(location, days))


@shared_task
def run_scheduled_demo_account_purge() -> bool:
    """Delete expired demo accounts. A no-op on any instance that is not the demo.

    Unconditionally scheduled (see ``CELERY_BEAT_SCHEDULE``) rather than
    registered only when ``UL_DEMO_MODE`` is on, matching every other entry
    there - the schedule is fixed at process start, and the individual task
    deciding whether it is due is the existing pattern (see
    ``run_scheduled_database_backup``). Harmless to fire on the real site: it
    checks the flag and returns immediately.

    Returns:
        True when this ran (this is the demo instance), False otherwise. The
        command itself logs how many accounts it purged.
    """
    from django.core.management import call_command

    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not app_settings.demo_mode:
        return False

    call_command("purge_demo_accounts", execute=True)
    return True


@shared_task
def run_scheduled_redata_public_locations_sync() -> bool:
    """Refresh the demo instance's location pool from REData. A no-op everywhere else.

    Unconditionally scheduled, same reasoning as
    ``run_scheduled_demo_account_purge``. REData's ``/public-locations/`` is
    not deployed anywhere reachable as of 2026-08-20, so this fires and finds
    nothing to import until that changes - once it does, the pool grows with
    no further action needed here.

    Returns:
        True when this ran (this is the demo instance), False otherwise.
    """
    from django.core.management import call_command

    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not app_settings.demo_mode:
        return False

    call_command("import_redata_public_locations")
    return True
