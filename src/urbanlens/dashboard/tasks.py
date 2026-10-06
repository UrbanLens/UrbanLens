"""Celery tasks for the dashboard app.

Tasks that hand untrusted uploaded bytes to a parser declare
``queue=SANDBOX_QUEUE`` (or ``SANDBOX_BATCH_QUEUE`` for a minutes-long batch
job), routing them to the isolated ``media-worker`` container rather than the
general-purpose worker. The queue is declared on the task, not at each
``apply_async`` site - see :mod:`urbanlens.dashboard.services.sandbox.queues`
for why, and :mod:`urbanlens.dashboard.services.sandbox.guard` for what the
isolation buys.

Every other task declares one of :attr:`Queue.INTERACTIVE`, :attr:`Queue.BULK`
or :attr:`Queue.MAINTENANCE`. The line is who waits - a person waiting on a
result or a safety deadline is interactive, a job sized by how much one
account owns is bulk, beat-driven site-wide work is maintenance -
``dashboard.checks.check_every_task_declares_a_queue`` fails startup for a
task that names none, since one on the default queue keeps working silently.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from channels.layers import get_channel_layer
from django.utils import timezone

from urbanlens.dashboard.services.ai.tasks import (  # noqa: F401 - celery's autodiscover_tasks() only imports <app>/tasks.py, so this is what registers the task on the worker
    run_assistant_turn_task,
)
from urbanlens.dashboard.services.core.capacity import ALBUM_PHOTOS, CapacityExceededError, ensure_room
from urbanlens.dashboard.services.core.celery import RetryNoticeError, update_task_progress
from urbanlens.dashboard.services.core.egress import external_background_task
from urbanlens.dashboard.services.core.locks import acquire_lock, beat_lock, release_lock
from urbanlens.dashboard.services.core.numbers import LATITUDE_BOUND, LONGITUDE_BOUND, coordinate_or_none
from urbanlens.dashboard.services.media.storage_errors import OBJECT_STORE_ERRORS, STORAGE_ERRORS, is_transient
from urbanlens.dashboard.services.pins import confirmed_import, import_preview
from urbanlens.dashboard.services.sandbox import sandbox_queue
from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.pins.pin_suggestions import LocationHit

logger = logging.getLogger(__name__)

#: Sandbox queue for untrusted parses; falls back to default when disabled.
SANDBOX_QUEUE = sandbox_queue()
#: Sandbox queue for minutes-long untrusted parses.
SANDBOX_BATCH_QUEUE = sandbox_queue(batch=True)

#: Limits for the five-minute safety sweeps, under their overlap lock (_CHECKIN_LOCK_TIMEOUT_SECONDS).
_CHECKIN_SOFT_TIME_LIMIT_SECONDS = 210
_CHECKIN_TIME_LIMIT_SECONDS = 240
#: Limits for the two-minute game stall sweeps, under their 110-second overlap locks.
_STALL_SWEEP_SOFT_TIME_LIMIT_SECONDS = 80
_STALL_SWEEP_TIME_LIMIT_SECONDS = 100


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def ensure_wiki_for_location(location_id: int) -> int | None:
    """Auto-create the Wiki for a Location when missing.

    Args:
        location_id: PK of the Location that just gained a pin.

    Returns:
        PK of the Wiki (new or pre-existing), or None if the Location no
        longer exists.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.core.bulk_followup import enqueue_follow_on
    from urbanlens.dashboard.services.core.celery import follow_on_queue
    from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("ensure_wiki_for_location: location %s no longer exists", location_id)
        return None

    wiki, created = Wiki.objects.get_or_create_for_location(location)
    # Enrichment sends the coordinate to outside providers, so it waits for an owner who allows that.
    consented = Pin.objects.filter(location=location, profile__external_apis_enabled=True).exists()
    if consented and (created or not boundary_generation_ran(location)):
        enqueue_follow_on(enrich_wiki_location, enrich_wiki_locations, wiki.pk, queue=follow_on_queue())
    if created:
        from urbanlens.dashboard.services.wiki.wiki_seed import seed_wiki_article_from_wikipedia

        seed_wiki_article_from_wikipedia(location)
    return wiki.pk


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def ensure_wikis_for_locations(location_ids: list[int]) -> list[int]:
    """Chunk-shaped sibling of :func:`ensure_wiki_for_location`, for a bulk import's fan-out (P109).

    Runs its own :func:`batching_follow_on_work` so the enrichment work a new wiki queues coalesces
    into :func:`enrich_wiki_locations` chunks too, whether this task was itself reached from inside an
    importer's own collector (nesting reuses it) or dispatched standalone.

    Args:
        location_ids: PKs of the Locations to ensure a Wiki for.

    Returns:
        PKs of the Wikis (new or pre-existing) - one per location that still existed.
    """
    from urbanlens.dashboard.services.core.bulk_followup import batching_follow_on_work

    wiki_pks: list[int] = []
    with batching_follow_on_work():
        for location_id in location_ids:
            try:
                wiki_pk = ensure_wiki_for_location(location_id)
            except OSError:
                # ensure_wiki_for_location is called directly, not dispatched, so its own
                # autoretry_for=(OSError,) can't schedule a delayed retry (Celery raises
                # immediately when request.called_directly is True) - re-raise so this task's
                # own autoretry retries the whole chunk instead of silently dropping the item.
                logger.warning("ensure_wikis_for_locations: transient failure on location %s, retrying chunk", location_id)
                raise
            except Exception:
                logger.exception("ensure_wikis_for_locations: location %s failed", location_id)
                continue
            if wiki_pk is not None:
                wiki_pks.append(wiki_pk)
    return wiki_pks


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def ensure_building_wikis(location_id: int) -> list[int]:
    """Give each pinned location on a campus's buildings its building's wiki, once the campus's building list is cached.

    A location pinned before the list landed was answered with the campus's wiki, which is not its own (P265).

    Args:
        location_id: PK of the Location whose building list was just cached.

    Returns:
        PKs of the wikis ensured; empty unless the Location holds a campus's wiki.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.building_wikis import locations_awaiting_building_wikis

    campus_wiki = Wiki.objects.filter(location_id=location_id, place__isnull=False).select_related("place", "location").first()
    if campus_wiki is None:
        return []
    awaiting = locations_awaiting_building_wikis(campus_wiki)
    return ensure_wikis_for_locations(awaiting) if awaiting else []


@shared_task(soft_time_limit=240, time_limit=270, bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.PANEL_FETCH)
def enrich_wiki_location(self, wiki_id: int) -> bool:
    """Enrich a Wiki's Location with place link, name, and boundaries.

    Args:
        wiki_id: PK of the Wiki to enrich.

    Returns:
        True when the wiki still existed and enrichment ran.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService
    from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, generate_unless_in_flight
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

    from urbanlens.dashboard.services.locations.naming import GOOGLE_PLACES_NAME_SOURCE, is_meaningful_name, update_location_name_from_external_sources
    from urbanlens.dashboard.services.wiki.wiki_naming import OFFICIAL_NAME_SOURCE, adopt_public_name

    try:
        update_location_name_from_external_sources(location)
    except Exception:
        logger.exception("enrich_wiki_location: cached name refresh failed for location %s", location.pk)
    wiki.refresh_from_db(fields=["name"])
    location.refresh_from_db(fields=["official_name", "official_name_source"])

    if not is_meaningful_name(wiki.name):
        place_name, source = location.provider_name, OFFICIAL_NAME_SOURCE
        if not is_meaningful_name(place_name):
            source = GOOGLE_PLACES_NAME_SOURCE
            try:
                place_name = name_resolver.resolve(float(location.latitude), float(location.longitude))
            except Exception:
                logger.exception("enrich_wiki_location: name resolution failed for location %s", location.pk)
                place_name = None
        adopt_public_name(wiki, place_name, source=source)

    update_task_progress(self, current=1, total=2, message="Generating boundaries...")
    if not boundary_generation_ran(location):
        # A new pin's bootstrap is usually drawing the same boundary right now.
        generate_unless_in_flight(location, name=wiki.name or None)

    update_task_progress(self, current=2, total=2, message="Wiki ready")
    return True


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def enrich_wiki_locations(self, wiki_ids: list[int]) -> dict[int, bool]:
    """Chunk-shaped sibling of :func:`enrich_wiki_location`, for a bulk import's fan-out (P109).

    Each wiki keeps its own place-linking/name/boundary failures isolated - autoretry on this task
    retries the whole chunk, so a wiki that keeps failing must not stop its chunk-mates from ever
    being enriched.

    Args:
        wiki_ids: PKs of the Wikis to enrich.

    Returns:
        Whether enrichment ran, per wiki id.
    """
    results: dict[int, bool] = {}
    total = len(wiki_ids)
    for index, wiki_id in enumerate(wiki_ids):
        update_task_progress(self, current=index, total=total, message=f"Enriching wiki {index + 1} of {total}...")
        try:
            results[wiki_id] = enrich_wiki_location(wiki_id)
        except OSError:
            # enrich_wiki_location is called directly, not dispatched, so its own
            # autoretry_for=(OSError,) can't schedule a delayed retry (Celery raises
            # immediately when request.called_directly is True) - re-raise so this task's
            # own autoretry retries the whole chunk instead of silently dropping the item.
            logger.warning("enrich_wiki_locations: transient failure on wiki %s, retrying chunk", wiki_id)
            raise
        except Exception:
            logger.exception("enrich_wiki_locations: wiki %s failed", wiki_id)
            results[wiki_id] = False
    update_task_progress(self, current=total, total=total, message="Batch enrichment complete")
    return results


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def mirror_buildings_to_wiki(pin_id: int, selection_keys: list[str]) -> int:
    """Mirror imported buildings onto the community wiki off-request.

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def auto_nest_building_pins(pin_id: int) -> int:
    """Build a new pin's default child-pin structure from cached buildings.

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


# No autoretry: each stage's fetch owns its failure policy (run_panel_fetch's suppression markers), and a stage that
# fails still queues the next one. The limits sit under the bootstrap's own flight-marker TTL.
@shared_task(soft_time_limit=240, time_limit=270, queue=Queue.INTERACTIVE)
def bootstrap_location(pin_id: int, stage: str = "boundary", attempt: int = 0) -> str | None:
    """Run one stage of a new root pin's property bootstrap, then queue the next (``services.pins.bootstrap``).

    Queued on root-pin creation; queue it by hand to bootstrap any root pin on demand.

    Args:
        pin_id: PK of the root pin.
        stage: A ``BootstrapStage`` value; the chain starts at the first.
        attempt: How many times this stage has waited on another caller's fetch.

    Returns:
        The stage that ran, or None when the pin is gone or the stage unknown.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.bootstrap import BootstrapStage, enqueue_step, run_stage

    try:
        current = BootstrapStage(stage)
    except ValueError:
        logger.warning("bootstrap_location: unknown stage %r for pin %s", stage, pin_id)
        return None
    pin = Pin.objects.select_related("location", "profile__user").filter(pk=pin_id).first()
    if pin is None or pin.location_id is None:
        return None
    step = run_stage(pin, current, attempt)
    if step is not None:
        enqueue_step(pin_id, step)
    return current


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def generate_boundaries_for_location(location_id: int, *, force: bool = False, attempt: int = 0) -> bool:
    """Generate or refresh default boundaries for a Location.

    Args:
        location_id: PK of the Location.
        force: Re-run the provider chain even when the coordinate already resolves - a retry after the
            authoritative provider deferred and a fallback answered meanwhile.
        attempt: How many deferred retries preceded this run.

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
        if force or not ran or stale:
            generate_location_boundaries(location, force=force, attempt=attempt)
        return True
    finally:
        cache.delete(generation_lock_key(location_id))


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def classify_detail_marker(kind: str, marker_id: int) -> bool:
    """Decide whether a newly placed child pin/wiki stands on a building.

    Queued whenever a sub-marker is created or moved without the user
    choosing a type themselves (see ``controllers.detail_pins``). Generating
    the marker's own boundaries first is the whole point: the provider chain
    only fills a location's ``BUILDING`` boundary when some provider has a
    footprint polygon containing that exact point.

    Runs on the interactive (prefork) queue rather than ``panel_fetch``:
    boundary generation does real CPU-bound geometry work, and a campus
    import queues one of these per building. See ``PanelSource.queue`` for
    the same reasoning.

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def refresh_profile_map_center(profile_id: int) -> bool:
    """Recompute one profile's cached map centre away from the request that made it stale.

    Args:
        profile_id: PK of the ``Profile`` whose centre a new pin outdated.

    Returns:
        True when a centre was computed, False when the profile is gone or owns no locatable pins.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        return False
    return profile.refresh_map_center() is not None


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def push_trip_to_calendar(trip_id: int) -> int:
    """Push a changed trip to its auto-synced calendars.

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


#: An auto-sync request older than this lost its push, or its push failed.
PENDING_CALENDAR_PUSH_AGE = timedelta(minutes=10)
#: Failed pushes after which a request is dropped until the trip changes again.
MAX_CALENDAR_PUSH_ATTEMPTS = 5
PENDING_CALENDAR_PUSH_BATCH = 200


@shared_task(queue=Queue.MAINTENANCE)
@external_background_task("calendar-push-sweep")
def requeue_pending_calendar_pushes() -> int:
    """Queue the auto-sync pushes whose trip change was never delivered to the calendar.

    Returns:
        How many trips were queued.
    """
    from urbanlens.dashboard.models.calendar_sync.model import TripCalendarLink
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    cutoff = timezone.now() - PENDING_CALENDAR_PUSH_AGE
    pending = TripCalendarLink.objects.filter(activity__isnull=True, auto_sync=True, push_requested_at__lt=cutoff)
    abandoned = pending.filter(push_attempts__gte=MAX_CALENDAR_PUSH_ATTEMPTS).update(push_requested_at=None, push_attempts=0)
    if abandoned:
        logger.warning("Dropped %d calendar auto-sync request(s) after %d failed pushes", abandoned, MAX_CALENDAR_PUSH_ATTEMPTS)
    trip_ids = list(pending.order_by("trip_id").values_list("trip_id", flat=True).distinct()[:PENDING_CALENDAR_PUSH_BATCH])
    queued = 0
    for trip_id in trip_ids:
        # A refusal is found again by the next sweep.
        if safely_enqueue_task(push_trip_to_calendar, trip_id, durable=False) is not None:
            queued += 1
    if queued:
        logger.info("Re-queued %d calendar auto-sync push(es)", queued)
    return queued


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def run_user_data_export(self, user_id: int, export_types: list[str], export_dir: str, base_url: str, job_id: str | None = None, email_to_user: bool = False) -> bool:  # noqa: PLR0917 - a Celery task: safely_enqueue_task passes its arguments positionally, and queued messages carry that order
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


@shared_task(queue=Queue.BULK)
def cleanup_export_artifacts_task(export_dir: str, job_id: str | None = None) -> None:
    """Remove expired export artifacts and cache-backed status."""
    from urbanlens.dashboard.services.import_export.export import ExportJobStatus, cleanup_export_artifacts

    cleanup_export_artifacts(export_dir, ExportJobStatus(job_id) if job_id else None)
    logger.info("Cleaned up export artifacts for job %s", job_id or export_dir)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, max_retries=None, queue=SANDBOX_BATCH_QUEUE)
def run_user_data_import(self, user_id: int, zip_path: str, job_id: str, resume: dict[str, Any] | None = None, storage_waits: int = 0) -> bool:
    """Parse a UrbanLens export ZIP and import data for the user.

    Runs again, later, for files storage refused (``import_data.ImportWaitingForStorageError``).

    Args:
        user_id: PK of the importing user.
        zip_path: The uploaded archive.
        job_id: The import job whose status the page polls.
        resume: The tally and deferred files of the runs before a storage wait.
        storage_waits: Storage waits in a row so far.

    Returns:
        Whether the import succeeded, even partly.
    """
    from urbanlens.dashboard.services.import_export.import_data import ImportWaitingForStorageError, run_import

    logger.info("Starting data import for user %s, job %s", user_id, job_id)
    update_task_progress(self, current=0, total=1, message="Preparing import...")
    try:
        success = run_import(user_id, zip_path, job_id, resume=resume, storage_waits=storage_waits)
    except ImportWaitingForStorageError as waiting:
        logger.info("Data import for user %s, job %s waits %ss for storage", user_id, job_id, waiting.countdown)
        raise self.retry(
            args=(user_id, zip_path, job_id),
            kwargs={"resume": waiting.resume, "storage_waits": waiting.storage_waits},
            countdown=waiting.countdown,
            exc=RetryNoticeError(waiting.message),
        ) from waiting
    if success:
        update_task_progress(self, current=1, total=1, message="Import complete")
        logger.info("Finished data import for user %s, job %s", user_id, job_id)
        return True
    update_task_progress(self, current=1, total=1, message="Import failed")
    logger.warning("Data import failed for user %s, job %s", user_id, job_id)
    return False


@shared_task(queue=Queue.BULK)
def cleanup_import_artifacts_task(import_dir_path: str, job_id: str | None = None) -> None:
    """Remove expired import artifacts and cache-backed status."""
    from urbanlens.dashboard.services.import_export.import_data import ImportJobStatus, cleanup_import_artifacts

    cleanup_import_artifacts(import_dir_path, ImportJobStatus(job_id) if job_id else None)
    logger.info("Cleaned up import artifacts for job %s", job_id or import_dir_path)


@shared_task(queue=Queue.BULK, soft_time_limit=confirmed_import.SOFT_TIME_LIMIT_SECONDS, time_limit=confirmed_import.TIME_LIMIT_SECONDS)
def run_confirmed_pin_import(profile_id: int, job_id: str) -> dict[str, Any]:
    """Run one account's confirmed pin import from the selection stored under *job_id*."""
    return confirmed_import.run_confirmed_import(profile_id, job_id)


@shared_task(
    bind=True,
    queue=SANDBOX_QUEUE,
    soft_time_limit=import_preview.PARSE_SOFT_TIME_LIMIT_SECONDS,
    time_limit=import_preview.PARSE_TIME_LIMIT_SECONDS,
    max_retries=import_preview.PARSE_SLOT_MAX_RETRIES,
)
def parse_import_preview_task(self, profile_id: int, job_id: str) -> None:
    """Read an import preview's uploaded files in the sandbox worker, once a site-wide slot is free."""
    if not import_preview.parse_import_preview(profile_id, job_id):
        raise self.retry(countdown=import_preview.PARSE_SLOT_RETRY_SECONDS)


@shared_task(soft_time_limit=import_preview.FINISH_SOFT_TIME_LIMIT_SECONDS, time_limit=import_preview.FINISH_TIME_LIMIT_SECONDS, queue=Queue.INTERACTIVE)
def finish_import_preview_task(profile_id: int, job_id: str) -> None:
    """Finish the part of an import preview that needs the network."""
    import_preview.finish_import_preview(profile_id, job_id)


@shared_task(bind=True, queue=SANDBOX_QUEUE, max_retries=5)
def publish_held_upload(self, key: str, pk: int, held_name: str) -> bool:
    """Re-encode a held icon or avatar in the sandbox worker and show it.

    Storage failing past the task's own retries leaves the upload held, waiting for :func:`retry_waiting_uploads`.
    """
    from urbanlens.dashboard.services.media import upload_retry
    from urbanlens.dashboard.services.media.held_upload import drop_held, publish_held

    try:
        published = publish_held(key, pk, held_name, attempt=self.request.id)
    except STORAGE_ERRORS as exc:
        if upload_retry.means_file_is_gone(exc):
            if upload_retry.file_is_gone(key, pk, held_name, exc):
                logger.warning("Dropping the upload held for %s %s: storage has not had its file for %s", key, pk, upload_retry.GONE_GRACE)
                drop_held(key, pk, held_name)
                upload_retry.stop_waiting(key, pk)
            return False
        if self.request.retries < self.max_retries and not upload_retry.is_waiting(key, pk):
            raise self.retry(exc=exc, countdown=min(60 * (2**self.request.retries), 900)) from exc
        logger.warning("Storage could not read or write the upload held for %s %s; it waits for storage", key, pk, exc_info=True)
        upload_retry.wait_for_storage(key, pk, held_name, exc)
        return False
    upload_retry.stop_waiting(key, pk)
    if published:
        upload_retry.record_storage_success()
    return published


@shared_task(queue=Queue.MAINTENANCE)
def measure_media_usage_task() -> float:
    """Measure the media volume for the site-admin system panel."""
    from urbanlens.dashboard.services.admin.media_usage import measure_media_usage

    return measure_media_usage().megabytes


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def cleanup_vestigial_assets_task() -> dict[str, int]:
    """Sweep stale import/export artifacts missed by per-job cleanup tasks."""
    from urbanlens.dashboard.services.import_export.vestigial_assets import cleanup_vestigial_assets

    result = cleanup_vestigial_assets()
    if result.total < 1:
        logger.debug("No vestigial assets found")
    else:
        logger.info("Vestigial asset cleanup complete: %s", result.as_dict())
    return result.as_dict()


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def build_map_document(profile_id: int) -> int:
    """Build and cache one profile's map document.

    Off the request path because building it holds the whole document in memory, which is exactly what
    the streaming response exists to avoid.

    Args:
        profile_id: Whose document to build.

    Returns:
        Bytes stored, or 0 when it was already cached, too large, or the profile is gone.
    """
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.map_pins import document as map_document
    from urbanlens.dashboard.services.map_pins.view_urls import with_view_urls

    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        return 0
    query = Pin.objects.filter(profile=profile).root_pins().select_related("location")
    return map_document.build_and_store(profile, query, decorate=with_view_urls)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def suggest_wiki_categories(wiki_ids: list[int]) -> dict[int, list[str]]:
    """Chunk-shaped sibling of :func:`suggest_wiki_category`, for a bulk import's fan-out (P109).

    Args:
        wiki_ids: PKs of the Wikis to suggest and attach labels for.

    Returns:
        The names of the labels attached, per wiki id.
    """
    results: dict[int, list[str]] = {}
    for wiki_id in wiki_ids:
        try:
            results[wiki_id] = suggest_wiki_category(wiki_id)
        except OSError:
            # suggest_wiki_category is called directly, not dispatched, so its own
            # autoretry_for=(OSError,) can't schedule a delayed retry (Celery raises
            # immediately when request.called_directly is True) - re-raise so this task's
            # own autoretry retries the whole chunk instead of silently dropping the item.
            logger.warning("suggest_wiki_categories: transient failure on wiki %s, retrying chunk", wiki_id)
            raise
        except Exception:
            logger.exception("suggest_wiki_categories: wiki %s failed", wiki_id)
            results[wiki_id] = []
    return results


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def suggest_pin_categories(pin_ids: list[int]) -> dict[int, list[str]]:
    """Chunk-shaped sibling of :func:`suggest_pin_category`, for a bulk import's fan-out (P109).

    Args:
        pin_ids: PKs of the Pins to suggest and attach labels for.

    Returns:
        The names of the labels attached, per pin id.
    """
    results: dict[int, list[str]] = {}
    for pin_id in pin_ids:
        try:
            results[pin_id] = suggest_pin_category(pin_id)
        except OSError:
            # suggest_pin_category is called directly, not dispatched, so its own
            # autoretry_for=(OSError,) can't schedule a delayed retry (Celery raises
            # immediately when request.called_directly is True) - re-raise so this task's
            # own autoretry retries the whole chunk instead of silently dropping the item.
            logger.warning("suggest_pin_categories: transient failure on pin %s, retrying chunk", pin_id)
            raise
        except Exception:
            logger.exception("suggest_pin_categories: pin %s failed", pin_id)
            results[pin_id] = []
    return results


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def resolve_location_place_name(location_id: int) -> str | None:
    """Fetch and cache a Location's Google place name outside the request/response cycle.

    Location.place_name is deliberately cache-only (see its docstring) - this is what actually populates
    that cache, dispatched from wherever a missing place name is first noticed (e.g. PinController.view)
    so the next render of this Location, by any pin/user sharing its coordinates, finds it warm.
    """
    from urbanlens.dashboard.models.location.model import Location

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("resolve_location_place_name: location %s no longer exists", location_id)
        return None
    return location.get_place_name()


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def backfill_location_address(location_id: int) -> bool:
    """Reverse-geocode and persist a Location's street address outside the request/response cycle.

    The background counterpart to ``resolve_location_place_name`` for address components:
    ``ensure_location_address`` makes a live Google Geocoding call, so it must never run inline on a
    page render - PinOverviewView dispatches this instead when it notices a route-less location, and the
    next render (by any pin/user sharing this Location) reads the backfilled row straight from the DB.

    Args:
        location_id: PK of the Location to backfill.

    Returns:
        True when at least one address component was written.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
    from urbanlens.dashboard.services.locations.addresses import ensure_location_address

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("backfill_location_address: location %s no longer exists", location_id)
        return False
    try:
        return ensure_location_address(location)
    except RequestCancelledError as exc:
        logger.info("backfill_location_address: geocoding refused for location %s, left for a later view: %s", location_id, exc)
        return False


def _archive_link_to_wayback(link_model: str, link_id: int) -> bool:
    """Archive one link, for :func:`archive_link_to_wayback` and its single-id siblings.

    Args:
        link_model: ``"PinLink"`` or ``"WikiLink"``.
        link_id: PK of the link row to archive.

    Returns:
        True when this link was given a wayback_url, False otherwise.
    """
    from urbanlens.dashboard.services.links.wayback_archive import ArchiveOutcome, archive_link

    return archive_link(link_model, link_id) is ArchiveOutcome.ARCHIVED


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def archive_link_to_wayback(link_model: str, link_id: int) -> bool:
    """Best-effort archive a link URL to the Wayback Machine.

    Args:
        link_model: ``"PinLink"`` or ``"WikiLink"``.
        link_id: PK of the link row to archive.

    Returns:
        True when a wayback_url was saved, False otherwise.
    """
    return _archive_link_to_wayback(link_model, link_id)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def archive_pin_link_to_wayback(link_id: int) -> bool:
    """Single-id ``PinLink`` entry point, fitting :func:`enqueue_follow_on`'s one-argument contract."""
    return _archive_link_to_wayback("PinLink", link_id)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def archive_pin_links_to_wayback(link_ids: list[int]) -> dict[int, bool]:
    """Chunk-shaped sibling of :func:`archive_pin_link_to_wayback`, for a bulk import's fan-out (P109).

    A confirmed import that extracts embedded links from each pin's raw description
    (``maps.py::_attach_description_extras``) creates one ``PinLink`` per link, each of which
    otherwise queues its own Wayback-archive task. Links left when the Archive refuses for now are
    taken up by :func:`sweep_unarchived_links`.

    Args:
        link_ids: PKs of the PinLinks to archive.

    Returns:
        Whether archiving saved a wayback_url, per link id.
    """
    from urbanlens.dashboard.services.links.wayback_archive import archive_links

    return archive_links("PinLink", link_ids)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def archive_wiki_link_to_wayback(link_id: int) -> bool:
    """Single-id ``WikiLink`` entry point, fitting :func:`enqueue_follow_on`'s one-argument contract."""
    return _archive_link_to_wayback("WikiLink", link_id)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def archive_wiki_links_to_wayback(link_ids: list[int]) -> dict[int, bool]:
    """Chunk-shaped sibling of :func:`archive_wiki_link_to_wayback`, for a bulk import's fan-out (P109).

    Args:
        link_ids: PKs of the WikiLinks to archive.

    Returns:
        Whether archiving saved a wayback_url, per link id.
    """
    from urbanlens.dashboard.services.links.wayback_archive import archive_links

    return archive_links("WikiLink", link_ids)


#: Longer than a sweep's soft limit, so a run that overruns still holds off the next.
_WAYBACK_SWEEP_LOCK_SECONDS = 660


@shared_task(soft_time_limit=600, time_limit=_WAYBACK_SWEEP_LOCK_SECONDS, queue=Queue.MAINTENANCE)
@external_background_task("wayback-archive-sweep")
def sweep_unarchived_links(limit: int | None = None) -> dict[str, int]:
    """Ask the Wayback Machine again about links it has not archived yet (P308).

    Takes up a URL whose wait after a failure has passed, and a link whose own task never ran.

    Args:
        limit: The most URLs to ask about; the service's batch by default.

    Returns:
        How many links came to each outcome.
    """
    from urbanlens.dashboard.services.core.locks import beat_lock
    from urbanlens.dashboard.services.links.wayback_archive import SWEEP_BATCH, sweep

    with beat_lock("urbanlens:wayback-archive:sweep-lock", _WAYBACK_SWEEP_LOCK_SECONDS) as acquired:
        if not acquired:
            return {}
        return {str(outcome): count for outcome, count in sweep(limit or SWEEP_BATCH).items()}


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def prefetch_location_external_data(location_id: int, google_place_id: str | None = None, profile_id: int | None = None, pin_id: int | None = None) -> None:
    """Pre-warm LocationCache for a newly created Location.

    Runs Wikipedia and NPS lookups so that the first time a user opens the pin detail page the data is
    already cached. Every lookup is driven by the Location's public data, never the pin's own name.

    Args:
        location_id: PK of the Location to prefetch data for.
        google_place_id: Optional Google Places place_id already resolved by the caller; used to copy
        existing Django-cache data into LocationCache.
        profile_id: PK of the profile whose action enqueued this task, if any - used to honor that
        profile's name-source priority override.
        pin_id: PK of the pin just created here, if any. A match cached before it existed was never
        seeded into it, since pin articles are seeded only when a write turns a miss into a match.
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

    # Resolves the address first when there is none, which a coordinate-only pin's match depends on.
    if LocationCache.get_fresh(location, "wikipedia") is None:
        try:
            from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

            WikipediaEnrichmentSource().enrich(location)
            logger.info("prefetch_location_external_data: cached Wikipedia for location %s", location_id)
        except Exception:
            logger.exception("prefetch_location_external_data: Wikipedia lookup failed for location %s", location_id)

    if pin_id is not None:
        _seed_new_pin_from_cached_wikipedia(location, pin_id)

    from urbanlens.dashboard.plugins.builtin.nps import NpsPanelSource
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

    if redata_configured() and LocationCache.get_fresh(location, NpsPanelSource.cache_source, max_age=NpsPanelSource.cache_max_age) is None:
        try:
            from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

            park = RedataNationalParksGateway().find_nearest_park(lat, lng)
            LocationCache.set(location, "nps", park or {}, query_key=f"{lat:.5f},{lng:.5f}")
            logger.info("prefetch_location_external_data: cached NPS for location %s", location_id)
        except Exception:
            logger.exception("prefetch_location_external_data: NPS lookup failed for location %s", location_id)

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

    try:
        update_location_name_from_external_sources(location, profile=profile)
    except Exception:
        logger.exception("prefetch_location_external_data: name refresh failed for location %s", location_id)


def _seed_new_pin_from_cached_wikipedia(location: Location, pin_id: int) -> None:
    """Give a new pin the article and link its location's cached Wikipedia match offers.

    Args:
        location: The pin's location.
        pin_id: PK of the new pin.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.wiki.wiki_seed import seed_pin_from_cached_wikipedia

    if (pin := Pin.objects.select_related("profile", "location").filter(pk=pin_id, location=location).first()) is not None:
        seed_pin_from_cached_wikipedia(pin)


@dataclass
class _UploadProcessResult:
    """Fields produced by one media-type processing step."""

    update_fields: dict[str, object]
    coords: tuple[float, float] | None = None
    new_stored_size: int | None = None
    superseded_name: str | None = None


def _process_photo_upload(image: Image, image_id: int, strip_location: bool, max_dimension_override: int | None = None) -> _UploadProcessResult | None:
    """Extract photo metadata and downscale; None on unreadable file.

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
        extract_embedded_keywords,
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
    from urbanlens.dashboard.services.media.storage import get_stored_photo_policy

    try:
        with image.image.open("rb") as image_file:
            coords = None if strip_location else extract_gps_coords(image_file)
            direction = None if strip_location else extract_gps_direction(image_file)
            altitude = None if strip_location or image.exif_altitude is not None else extract_gps_altitude(image_file)
            orientation = None if strip_location or image.exif_pitch is not None else extract_gps_orientation(image_file)
            taken_at = extract_taken_at(image_file)
            checksum = compute_checksum(image_file) if not image.checksum else None
            exif_data = extract_exif_data(image_file) if image.exif_data is None else None
            embedded_keywords = extract_embedded_keywords(image_file) if image.embedded_keywords is None else None
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

    if strip_location and exif_data:
        exif_data.pop("GPSInfo", None)

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
    if embedded_keywords is not None:
        image.embedded_keywords = embedded_keywords
        update_fields["embedded_keywords"] = embedded_keywords
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
    superseded_name: str | None = None
    max_dimension, convert_webp = get_stored_photo_policy(image, max_dimension_override)
    try:
        replacement = downscale_stored_image(image, max_dimension, convert_webp)
    except OBJECT_STORE_ERRORS:
        # Also OSError for a timeout, but says nothing about the photo; process_image_upload waits for storage.
        raise
    except (OSError, ValueError, EOFError, SyntaxError, PILDecompressionBombError) as exc:
        # DecompressionBombError derives from Exception alone, so it needs naming.
        logger.warning("Re-encoding failed for image %s: %s", image_id, exc, exc_info=True)
        if image.pending_scan:
            # Publishing a fresh upload that was never re-encoded would publish whatever metadata it carries.
            return None
    else:
        if replacement is not None:
            update_fields["image"] = image.image.name
            new_stored_size = replacement.size
            superseded_name = replacement.superseded_name

    try:
        if write_image_thumbnail(image):
            update_fields["thumbnail"] = image.thumbnail.name
    except (*OBJECT_STORE_ERRORS, OSError, ValueError, PILDecompressionBombError) as exc:
        # A miss here is retried by the hourly backfill_image_thumbnails sweep
        logger.warning("Thumbnail generation failed for image %s: %s", image_id, exc, exc_info=True)

    try:
        if write_image_marker_thumbnail(image):
            update_fields["marker_thumbnail"] = image.marker_thumbnail.name
    except (*OBJECT_STORE_ERRORS, OSError, ValueError, PILDecompressionBombError) as exc:
        # A miss here is retried by the hourly backfill_image_marker_thumbnails sweep
        logger.warning("Marker thumbnail generation failed for image %s: %s", image_id, exc, exc_info=True)

    try:
        if write_image_analysis_thumbnail(image):
            update_fields["analysis_thumbnail"] = image.analysis_thumbnail.name
    except (*OBJECT_STORE_ERRORS, OSError, ValueError, PILDecompressionBombError) as exc:
        # A miss here is retried by the hourly backfill_image_analysis_thumbnails sweep. Keywording skips a
        # photo that has no analysis copy rather than decoding one itself - see services.photos.photo_keywords.
        logger.warning("Analysis thumbnail generation failed for image %s: %s", image_id, exc, exc_info=True)

    return _UploadProcessResult(update_fields, coords, new_stored_size, superseded_name)


def _process_video_upload(image: Image, strip_location: bool) -> _UploadProcessResult:
    """Video-specific metadata extraction (via ffprobe) and downscaling (via ffmpeg)."""
    from urbanlens.dashboard.services.media.storage import get_video_downscale_policy
    from urbanlens.dashboard.services.media.videos import process_uploaded_video

    max_height = get_video_downscale_policy(image.profile) if image.profile is not None else None
    # The container's own location tags are always removed from the stored file; strip_location decides only
    # whether the coordinates are recorded on the row, where the app's visibility rules govern them.
    metadata, replacement = process_uploaded_video(image, max_height)

    update_fields: dict[str, object] = {}
    coords: tuple[float, float] | None = None
    if not strip_location:
        if "taken_at" in metadata:
            image.taken_at = metadata["taken_at"]
            update_fields["taken_at"] = image.taken_at
        latitude = coordinate_or_none(metadata.get("latitude"), bound=LATITUDE_BOUND)
        longitude = coordinate_or_none(metadata.get("longitude"), bound=LONGITUDE_BOUND)
        if latitude is not None and longitude is not None:
            coords = (latitude, longitude)
    if replacement is not None:
        update_fields["image"] = image.image.name
    return _UploadProcessResult(
        update_fields,
        coords,
        replacement.size if replacement else None,
        replacement.superseded_name if replacement else None,
    )


def _process_document_upload(image: Image, image_id: int) -> _UploadProcessResult:
    """Document-specific PDF conversion and OCR text extraction."""
    from urbanlens.dashboard.services.media.documents import convert_to_pdf, extract_pdf_text

    update_fields: dict[str, object] = {}
    try:
        replacement = convert_to_pdf(image)
    except OBJECT_STORE_ERRORS:
        raise
    except (OSError, ValueError) as exc:
        logger.warning("Document conversion failed for image %s: %s", image_id, exc, exc_info=True)
        replacement = None
    if replacement is not None:
        update_fields["image"] = image.image.name

    ocr_text = extract_pdf_text(image)
    if ocr_text:
        image.ocr_text = ocr_text
        update_fields["ocr_text"] = ocr_text
    return _UploadProcessResult(
        update_fields,
        None,
        replacement.size if replacement else None,
        replacement.superseded_name if replacement else None,
    )


def _sync_deduped_siblings(image: Image) -> None:
    """Copy the processed file and its metadata onto this user's other rows of the same bytes.

    ``attach_deduped_copy`` gives a fresh sibling the same ``pending_scan`` its original had at that
    moment (so it isn't immediately visible in its own, different pin/wiki while the shared file is
    still raw) - but a dedup sibling never runs this task itself, so this is the only place anything
    ever clears it again.
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
        "embedded_keywords": image.embedded_keywords,
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

    It runs here instead, which is what makes an upload return immediately; ``Image.pending_scan`` is
    what makes that safe, since nobody but the uploader can see the row until this clears it.

    Args:
        task: The bound Celery task, for ``retry``.
        image: The pending row whose stored file to scan.

    Returns:
        True when the file is clean and processing should continue.

    Raises:
        celery.exceptions.Retry: clamd was unreachable and retries remain.
        UploadStorageFailedError: Storage could not open or read the upload.
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
        if isinstance(exc.__cause__, OBJECT_STORE_ERRORS):
            # The scanner reports a failed read of its stream as itself being down.
            raise UploadStorageFailedError from exc.__cause__
        if task.request.retries < task.max_retries:
            # A clamd hiccup must not reject somebody's photo. Same backoff the comment scan uses; the upload
            # stays pending (invisible to anyone else) for as long as this takes.
            raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc
        logger.exception("Malware scan permanently unavailable for image %s after %s retries", image.pk, task.request.retries)
        _reject_image_upload(image, "Our antivirus scanner was unavailable, so this upload could not be checked and was removed. Please try again.")
        return False
    except STORAGE_ERRORS as exc:
        raise UploadStorageFailedError from exc

    if malware_error:
        _reject_image_upload(image, malware_error)
        return False
    return True


class UploadStorageFailedError(Exception):
    """Storage could not read or write an upload being processed, which says nothing about the upload itself."""


def _image_storage_failed(task, image: Image, name: str, exc: BaseException) -> bool:
    """Retry an upload storage failed on, then leave it waiting for storage; reject a pending one once its file is gone.

    Args:
        task: The bound ``process_image_upload``.
        image: The row being processed.
        name: The stored name it failed on.
        exc: What storage raised.

    Returns:
        False: the upload was not processed.

    Raises:
        celery.exceptions.Retry: While the task has retries left and the upload is not already waiting.
    """
    from urbanlens.dashboard.services.media import upload_retry

    target = upload_retry.IMAGE
    if upload_retry.means_file_is_gone(exc):
        if upload_retry.file_is_gone(target, image.pk, name, exc):
            logger.warning("Giving up on image %s: storage has not had its file for %s", image.pk, upload_retry.GONE_GRACE)
            upload_retry.stop_waiting(target, image.pk)
            if image.pending_scan:
                _reject_image_upload(image, "This upload's file could not be found in storage, so it was removed. You can try uploading it again.")
        return False
    if task.request.retries < task.max_retries and not upload_retry.is_waiting(target, image.pk):
        raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc
    logger.warning("Storage could not read or write image %s; it waits for storage", image.pk, exc_info=exc)
    upload_retry.wait_for_storage(target, image.pk, name, exc)
    return False


def _reject_image_upload(image: Image, reason: str) -> None:
    """Notify an uploader their upload was rejected, and remove it.

    Also rejects every dedup sibling pointing at the same stored file (``attach_deduped_copy`` copies
    ``pending_scan`` from its original at creation, but nothing besides *this* function ever runs on a
    sibling row - unlike :func:`_sync_deduped_siblings`, which only fires on success - so leaving them
    would strand each one hidden forever with no path to either clearing or removal).

    Args:
        image: The still-pending ``Image`` - photo, video or document - that was condemned by the scan
        or whose processing failed permanently.
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

    When the uploader has turned off visit-history tracking (``track_pin_visits``), GPS is treated as
    sensitive rather than useful: it's never read into ``Image.latitude``/``longitude`` or the
    ``exif_data`` snapshot, the stored file's own embedded GPS tag is stripped where supported, and no
    visit suggestion is raised.

    Args:
        image_id: PK of the row to process.
        max_dimension: Longest-edge cap to downscale to when the row has **no profile** - a
        location-enrichment photo fetched from a provider...

    Returns:
        True when the row was processed.
    """
    from decimal import Decimal

    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.media import upload_retry
    from urbanlens.dashboard.services.media.images import discard_superseded_file
    from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit
    from urbanlens.dashboard.services.visits.visits import visit_logging_allowed

    update_task_progress(self, current=0, total=1, message="Processing upload metadata...")
    image = Image.objects.filter(pk=image_id).select_related("pin__location", "wiki__location", "profile").first()
    if image is None or not image.image.name:
        upload_retry.stop_waiting(upload_retry.IMAGE, image_id)
        return False
    stored_name = image.image.name

    try:
        if image.pending_scan and not _scan_pending_upload(self, image):
            # Infected, or unscannable after every retry. The row and its file are
            # already gone (_reject_image_upload); nothing left to process.
            return False

        # A profile with visit-history tracking off doesn't want its location trail reconstructible from any
        # uploaded media either - GPS coordinates are neither extracted into the DB nor left embedded in the stored
        # file below, and no visit suggestion is raised.
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
                    return False
                # A fresh upload's stored file could not be opened at all - _process_photo_upload's own try/except
                # swallows the OSError/ ValueError rather than raising, specifically so this task's
                # autoretry_for=(OSError,) never sees it and never retries on its own; explicit retry here scopes
                # that decision to this one failure.
                if self.request.retries < self.max_retries:
                    raise self.retry(countdown=min(60 * (2**self.request.retries), 900))
                # Retries exhausted.
                _reject_image_upload(image, "We couldn't process this photo, so it was removed. You can try uploading it again.")
                return False
            result = photo_result
    except UploadStorageFailedError as failed:
        return _image_storage_failed(self, image, stored_name, failed.__cause__ or failed)
    except OBJECT_STORE_ERRORS as exc:
        return _image_storage_failed(self, image, stored_name, exc)

    update_fields, coords = result.update_fields, result.coords

    if image.pending_scan:
        # Set at upload time - see Image.pending_scan.
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
    upload_retry.stop_waiting(upload_retry.IMAGE, image_id)
    upload_retry.record_storage_success()

    # Only now, with the row naming the processed file.
    discard_superseded_file(image, result.superseded_name)

    _sync_deduped_siblings(image)

    if not strip_location:
        maybe_suggest_photo_visit(image)

    # Keyword generation runs as its own task so a slow provider (AI vision, classifiers) never delays the
    # metadata/downscale pipeline above; it also deliberately runs after the downscale so providers read the
    # final file.
    if image.media_type == MediaKind.PHOTO:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task as _enqueue

        # A profile-less row (location enrichment, provider photos) has no uploader who opted into keyword
        # generation, and no gallery of their own for the keywords to be searched from - so it does not get the
        # (plugin-dependent, possibly billed) vision pass.
        if image.profile is not None and image.profile.generate_photo_keywords:
            _enqueue(generate_image_keywords, image_id)

        from urbanlens.dashboard.services.photos.redata_relevance import queue_photo_submission

        queue_photo_submission(image)

    update_task_progress(self, current=1, total=1, message="Upload metadata processed")
    return True


#: Seconds one REData extraction may take; it runs an AI model over a scanned form.
_CRIS_EXTRACTION_TIMEOUT_SECONDS = 180


@shared_task(queue=Queue.BULK, soft_time_limit=_CRIS_EXTRACTION_TIMEOUT_SECONDS * 4)
def extract_cris_attachments(location_id: int, resource_uuid: str, attachment_ids: list[int]) -> int:
    """Ask REData to extract photos from CRIS documents, and merge them into the location's cached CRIS payload.

    Args:
        location_id: PK of the Location whose ``cris_building_usn`` cache lists the attachments.
        resource_uuid: The CRIS resource the attachments belong to.
        attachment_ids: The document attachments to extract.

    Returns:
        How many attachments gained extracted images.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

    if not redata_configured():
        return 0
    gateway = RedataGateway()
    merged = 0
    for attachment_id in attachment_ids:
        try:
            result = gateway.extract_cultural_resource_attachment(resource_uuid, attachment_id, timeout=_CRIS_EXTRACTION_TIMEOUT_SECONDS)
        except PropertyRecordsUnavailableError:
            logger.debug("extract_cris_attachments: nothing extracted from attachment %s of %s", attachment_id, resource_uuid, exc_info=True)
            continue
        if images := result.get("extracted_images"):
            merged += _merge_cris_extraction(location_id, resource_uuid, attachment_id, images)
    return merged


@shared_task(soft_time_limit=110, time_limit=130, queue=Queue.MAINTENANCE)
def fill_cris_campus_details(location_id: int, attempt: int = 0) -> int:
    """Add to a campus's CRIS row the records its site pass left undetailed, queueing the next pass while any remain.

    Args:
        location_id: PK of the site's Location.
        attempt: How many passes ran before this one.

    Returns:
        How many records were added.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

    location = Location.objects.filter(pk=location_id).first()
    if location is None or not redata_configured():
        return 0
    source = CrisBuildingPanelSource()
    try:
        filled, remaining = source.fill_campus_details(location)
    except PropertyRecordsUnavailableError:
        logger.info("fill_cris_campus_details: REData's lookup failed for location %s", location_id, exc_info=True)
        filled, remaining = 0, 1
    if remaining:
        source.queue_campus_fill(location_id, attempt + 1)
    return filled


def _merge_cris_extraction(location_id: int, resource_uuid: str, attachment_id: int, images: list) -> int:
    """Write one attachment's extracted images into the cached CRIS payload, returning 1 if it was there to update."""
    from django.db import transaction

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    with transaction.atomic():
        row = LocationCache.objects.select_for_update().filter(location_id=location_id, source="cris_building_usn").first()
        if row is None:
            return 0
        data = dict(row.data or {})
        merged = 0
        for attachment in data.get("attachments") or []:
            if attachment.get("resource_uuid") == resource_uuid and attachment.get("id") == attachment_id:
                attachment["extracted_images"] = images
                merged = 1
        if merged:
            LocationCache.objects.filter(pk=row.pk).update(data=data)
    return merged


@shared_task(queue=SANDBOX_QUEUE)
def render_proxied_media(source_key: str, size: str, descriptor: dict[str, str]) -> bool:
    """Decode one file an in-app REData proxy serves and keep the rendering (``services.media.proxied_renders``).

    Args:
        source_key: The proxy's cache key for the original file.
        size: Which rendering, a key of ``proxied_renders.SIZES``.
        descriptor: Where the web process staged the original (``previews.stage_preview_source``).

    Returns:
        True when a rendering was kept.
    """
    from urbanlens.dashboard.services.media import proxied_renders
    from urbanlens.dashboard.services.media.previews import discard_preview_source, load_preview_source, render_preview

    try:
        source = load_preview_source(descriptor)
        if source is None:
            # Swept before this ran; the next view queues it again.
            proxied_renders.release(source_key, size)
            return False
        rendered = render_preview(*source, max_dimension=proxied_renders.SIZES[size])
        proxied_renders.finish(source_key, size, rendered)
        return rendered is not None
    finally:
        discard_preview_source(descriptor)


#: ``remote_copies.DOWNLOAD_TIMEOUT_SECONDS`` bounds each read, so a provider trickling bytes needs a bound on the whole.
_REMOTE_COPY_SOFT_TIME_LIMIT_SECONDS = 100


@shared_task(queue=Queue.INTERACTIVE, soft_time_limit=_REMOTE_COPY_SOFT_TIME_LIMIT_SECONDS, time_limit=_REMOTE_COPY_SOFT_TIME_LIMIT_SECONDS + 10)
def fetch_remote_image_copy(copy_id: int, *, waited: int = 0) -> bool:
    """Download a third-party image for its first copy and hand the bytes to the sandbox to decode.

    Runs on an ordinary worker because the sandbox has no egress. Nothing here parses the bytes. The download takes
    one of the site-wide ``remote_copies.DOWNLOAD_SLOTS``; while none is free the task queues itself again
    (``remote_copies.wait_for_download_slot``) rather than wait in the worker.

    Args:
        copy_id: The ``RemoteImageCopy`` being made.
        waited: Seconds this copy has already waited for a download slot.

    Returns:
        True when the bytes were staged and their render queued.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.previews import RemoteSourceTimeoutError, discard_preview_source, fetch_remote_source, stage_preview_source
    from urbanlens.dashboard.services.media.remote_copies import DOWNLOAD_TIMEOUT_SECONDS, MAX_REMOTE_COPY_SOURCE_BYTES, forgive_timeout, pending_marker, record_failure, release_download_slot, take_download_slot, wait_for_download_slot

    copy = RemoteImageCopy.objects.filter(pk=copy_id).first()
    if copy is None or copy.file.name:
        return False
    slot = take_download_slot(str(copy_id))
    if slot is None:
        wait_for_download_slot(copy, waited)
        return False
    timed_out = False
    try:
        fetched = fetch_remote_source(copy.source_url, max_bytes=MAX_REMOTE_COPY_SOURCE_BYTES, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except (RemoteSourceTimeoutError, SoftTimeLimitExceeded):
        fetched, timed_out = None, True
    finally:
        release_download_slot(slot, str(copy_id))
    queued = False
    try:
        if fetched is None:
            if not (timed_out and forgive_timeout(copy)):
                record_failure(copy)
            return False
        descriptor = stage_preview_source(f"copy_{copy.url_digest}", *fetched)
        if safely_enqueue_task(render_remote_image_copy, copy.pk, descriptor, durable=False) is None:
            discard_preview_source(descriptor)
            return False
        queued = True
        return True
    finally:
        # The render clears the mark once it is queued; any other way out leaves the next request free to start again.
        if not queued:
            cache.delete(pending_marker(copy.url_digest))


@shared_task(queue=SANDBOX_QUEUE)
def render_remote_image_copy(copy_id: int, descriptor: dict[str, str]) -> bool:
    """Decode one downloaded third-party image and keep its re-encoded copy.

    Args:
        copy_id: The ``RemoteImageCopy`` being made.
        descriptor: Where :func:`fetch_remote_image_copy` staged the source (``previews.stage_preview_source``).

    Returns:
        True when the copy was stored.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
    from urbanlens.dashboard.services.media.previews import discard_preview_source, load_preview_source, render_preview
    from urbanlens.dashboard.services.media.remote_copies import REMOTE_COPY_MAX_DIMENSION, REMOTE_COPY_TILE_DIMENSION, pending_marker, record_failure, store, wants_tile_copy

    copy = RemoteImageCopy.objects.filter(pk=copy_id).first()
    try:
        source = load_preview_source(descriptor)
        rendered = render_preview(*source, max_dimension=REMOTE_COPY_MAX_DIMENSION) if copy is not None and source is not None else None
        if copy is None:
            return False
        if rendered is None or source is None:
            record_failure(copy)
            return False
        tile = render_preview(*source, max_dimension=REMOTE_COPY_TILE_DIMENSION) if wants_tile_copy(rendered[0]) else None
        store(copy, *rendered, tile=tile)
        return True
    finally:
        discard_preview_source(descriptor)
        if copy is not None:
            cache.delete(pending_marker(copy.url_digest))


@shared_task(queue=SANDBOX_QUEUE)
def render_remote_tile(tile_id: int, descriptor: dict[str, str]) -> bool:
    """Decode one downloaded foreign map tile and keep its re-encoded copy.

    Args:
        tile_id: The ``RemoteTile`` being kept.
        descriptor: Where the web process staged the source (``previews.stage_preview_source``).

    Returns:
        True when the tile was stored.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.models.remote_tiles.model import RemoteTile
    from urbanlens.dashboard.services.map.remote_tiles import REMOTE_TILE_MAX_DIMENSION, pending_marker, record_failure, store
    from urbanlens.dashboard.services.media.previews import discard_preview_source, load_preview_source, render_preview

    tile = RemoteTile.objects.select_related("source").filter(pk=tile_id).first()
    try:
        source = load_preview_source(descriptor)
        rendered = render_preview(*source, max_dimension=REMOTE_TILE_MAX_DIMENSION) if tile is not None and source is not None else None
        if tile is None:
            return False
        if rendered is None:
            record_failure(tile)
            return False
        if not store(tile, *rendered):
            logger.warning("Tile source %s keeps as many tiles as it may; %s/%s/%s was not kept", tile.source_id, tile.z, tile.x, tile.y)
            record_failure(tile)
            return False
        return True
    finally:
        discard_preview_source(descriptor)
        if tile is not None:
            cache.delete(pending_marker(tile.source.template_digest, tile.z, tile.x, tile.y))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=SANDBOX_QUEUE)
def generate_image_thumbnails(image_ids: list[int]) -> int:
    """Fill in missing grid thumbnails for already-stored photos.

    Called from :func:`backfill_image_thumbnails` (and nowhere on a request path).

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
                fields = ["thumbnail", "updated"]
                if image.media_unreadable_at is not None:
                    # The file is back. Cleared in the same write, so a row
                    # cannot carry a scar that outlives the fault.
                    image.media_unreadable_at = None
                    fields.append("media_unreadable_at")
                image.save(update_fields=fields)
                written += 1
        except (*OBJECT_STORE_ERRORS, OSError, ValueError, PILDecompressionBombError) as exc:
            if is_transient(exc):
                logger.warning("Thumbnail generation stopped at image %s: storage failed: %s", image.pk, exc, exc_info=True)
                break
            # Recorded, not only logged: the sweep resets its cursor and comes
            # round again, so without this a row whose bytes are gone is retried
            # hourly forever (N22 H64). Written with `.update()` because the
            # in-memory row is mid-failure and must not be saved wholesale.
            Image.objects.filter(pk=image.pk).update(media_unreadable_at=timezone.now())
            logger.warning("Thumbnail generation failed for image %s: %s", image.pk, exc, exc_info=True)
    return written


#: Cache key for the exclusive pk cursor :func:`backfill_image_thumbnails` walks.
_THUMBNAIL_BACKFILL_CURSOR_KEY = "image-thumbnail-backfill-cursor"
#: Long enough that a beat outage does not restart a half-finished walk at pk 0.
_THUMBNAIL_BACKFILL_CURSOR_TTL = 7 * 24 * 60 * 60


#: How long a row may sit ``pending_scan`` before the sweep assumes its task was lost rather than merely slow.
STALLED_UPLOAD_AGE = timedelta(hours=6)

#: Bound on one sweep, so a large backlog is drained over several ticks rather
#: than dumped onto the sandbox worker at once.
STALLED_UPLOAD_BATCH = 100


@shared_task(queue=Queue.MAINTENANCE)
def sweep_stale_preview_sources() -> int:
    """Remove staged preview sources whose render never ran.

    ``render_proxied_media`` deletes its own source, so anything this finds is from an enqueue that
    failed - the broker was down when a tile was requested

    - leaving a file on the media volume nothing will ever read.

    Returns:
        How many files were removed.
    """
    from urbanlens.dashboard.services.media.previews import sweep_preview_sources

    removed = sweep_preview_sources()
    if removed:
        logger.info("Removed %s orphaned preview source file(s)", removed)
    return removed


@shared_task(queue=Queue.MAINTENANCE)
def sweep_held_uploads() -> int:
    """Re-queue stalled held icons and avatars, drop ones whose file is gone or whose publish never finishes, and remove held files nothing names.

    Deliberately not on the sandbox queue - it enqueues, it does not parse.

    Returns:
        How many held uploads were queued or dropped, plus how many files were removed.
    """
    from urbanlens.dashboard.services.media.held_upload import sweep_held_uploads as sweep

    handled, removed = sweep()
    if handled or removed:
        logger.info("Held uploads: %s queued or dropped, %s unnamed file(s) removed", handled, removed)
    return handled + removed


@shared_task(queue=Queue.MAINTENANCE)
def sweep_unnamed_files() -> int:
    """Delete icon, avatar and comment image files no row names, such as one whose delete storage refused.

    Returns:
        How many files were deleted.
    """
    from urbanlens.dashboard.services.media.stored_field import sweep_unnamed_files as sweep

    removed = sweep()
    if removed:
        logger.info("Removed %s stored file(s) no row names", removed)
    return removed


@shared_task(queue=Queue.MAINTENANCE)
def retry_waiting_uploads() -> int:
    """Queue a bounded batch of uploads waiting for storage whose next attempt is due, and report stuck ones.

    Returns:
        How many attempts were queued.
    """
    from urbanlens.dashboard.services.media.upload_retry import retry_waiting_uploads as retry

    queued = retry()
    if queued:
        logger.info("Queued %s upload(s) waiting for storage", queued)
    return queued


@shared_task(queue=Queue.MAINTENANCE)
def adopt_stalled_comment_scans() -> int:
    """Leave pending comments whose scan never ran to :func:`retry_waiting_uploads`.

    Returns:
        How many comments were taken up.
    """
    from urbanlens.dashboard.services.media.upload_retry import adopt_stalled_comment_scans as adopt

    adopted = adopt()
    if adopted:
        logger.info("%s pending comment scan(s) never ran and now wait for a retry", adopted)
    return adopted


#: Lock TTL for the outbox drain: above its hard time limit, below its 60-second beat interval.
_OUTBOX_DRAIN_LOCK_SECONDS = 55


@shared_task(queue=Queue.INTERACTIVE, soft_time_limit=40, time_limit=50)
def drain_task_outbox() -> int:
    """Queue the tasks the broker refused while it was down.

    Interactive, because what it replays is mostly interactive work (alerts, uploads, invitations) whose
    delay is a person waiting; the drain itself is a bounded batch of inserts.

    Returns:
        How many tasks were queued.
    """
    from urbanlens.dashboard.services.core.locks import beat_lock
    from urbanlens.dashboard.services.core.task_outbox import drain_outbox

    with beat_lock("urbanlens:task-outbox:drain-lock", _OUTBOX_DRAIN_LOCK_SECONDS) as acquired:
        if not acquired:
            return 0
        return drain_outbox()


@shared_task(queue=Queue.MAINTENANCE)
def requeue_stalled_pending_uploads(limit: int | None = None) -> int:
    """Re-enqueue uploads whose processing task never ran.

    Without a sweep that is permanent, and the uploader sees an upload that succeeded and then never
    appeared to anyone.

    Args:
        limit: Override the batch size.

    Returns:
        How many rows were re-enqueued.
    """
    from django.utils import timezone

    from urbanlens.dashboard.models.images.model import Image, QuotaExemption
    from urbanlens.dashboard.models.upload_retry import UploadRetry
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.upload_retry import IMAGE
    from urbanlens.dashboard.services.photos.photo_enrichment import enriched_max_dimension

    cutoff = timezone.now() - STALLED_UPLOAD_AGE
    batch = STALLED_UPLOAD_BATCH if limit is None else max(1, limit)
    # Deduplicated siblings are deliberately excluded.
    waiting = UploadRetry.objects.filter(target=IMAGE).values("object_id")
    stalled = list(
        Image.objects.processing().filter(created__lt=cutoff).exclude(quota_exempt_reason=QuotaExemption.DEDUPLICATED).exclude(pk__in=waiting).order_by("created").values_list("pk", "profile_id", "source", "upload_sweep_attempts")[:batch],
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
        # A profile-less row is a provider photo, and its cap lived only at the call site that created it -
        # recovered here from its source so the reprocessed file matches what it should have been, rather than
        # falling back to the generic default.
        max_dimension = None if profile_id is not None else enriched_max_dimension(source)
        Image.objects.filter(pk=image_id).update(upload_sweep_attempts=F("upload_sweep_attempts") + 1)
        safely_enqueue_task(process_image_upload, image_id, max_dimension)
        requeued += 1
    logger.info("Re-enqueued %s upload(s) still pending after %s (%s gave up)", requeued, STALLED_UPLOAD_AGE, len(stalled) - requeued)
    return requeued


@shared_task(queue=Queue.MAINTENANCE)
def discard_unretried_failed_uploads(limit: int | None = None) -> int:
    """Throw away failed uploads nobody came back for.

    Its filename is how the uploader recognises which picture went away, and once the bytes are gone it
    is the only trace.

    Args:
        limit: Override the batch size.

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

    If the original was deleted first (the user removed it, or it was rejected in a way that missed this
    sibling), nothing is left to do that - and unlike a real upload the sibling must not be run through
    the task itself, because the file it points at belongs to somebody else's row.

    Args:
        cutoff: Only siblings created before this are considered.

    Returns:
        How many rows were cleared.
    """
    from django.db.models import Exists, OuterRef

    from urbanlens.dashboard.models.images.model import Image, QuotaExemption

    has_source_row = Image.objects.filter(profile_id=OuterRef("profile_id"), checksum=OuterRef("checksum")).exclude(pk=OuterRef("pk")).exclude(quota_exempt_reason=QuotaExemption.DEDUPLICATED)
    orphaned = Image.objects.processing().filter(created__lt=cutoff, quota_exempt_reason=QuotaExemption.DEDUPLICATED).annotate(has_source=Exists(has_source_row)).filter(has_source=False)
    cleared = orphaned.update(pending_scan=False)
    if cleared:
        logger.info("Cleared %s dedup sibling(s) whose original no longer exists", cleared)
    return cleared


@shared_task(queue=Queue.MAINTENANCE)
def backfill_image_thumbnails(limit: int | None = None) -> int:
    """Enqueue a bounded batch of photos that still lack a grid thumbnail.

    Walks the table by primary key so a handful of unreadable files cannot stall the rest of the
    library: this tick's last id is the next tick's exclusive floor, and an exhausted cursor resets on a
    later tick rather than re-queueing the same in-flight batch immediately.

    Args:
        limit: Override the default batch size.

    Returns:
        How many image ids were queued (0 when the library is caught up, or this tick only reset the
        cursor).
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

    The marker-thumbnail mirror of :func:`generate_image_thumbnails` - see its docstring for why this
    exists alongside upload-time generation.

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
                fields = ["marker_thumbnail", "updated"]
                if image.media_unreadable_at is not None:
                    # The file is back. Cleared in the same write, so a row
                    # cannot carry a scar that outlives the fault.
                    image.media_unreadable_at = None
                    fields.append("media_unreadable_at")
                image.save(update_fields=fields)
                written += 1
        except (*OBJECT_STORE_ERRORS, OSError, ValueError, PILDecompressionBombError) as exc:
            if is_transient(exc):
                logger.warning("Marker thumbnail generation stopped at image %s: storage failed: %s", image.pk, exc, exc_info=True)
                break
            # Recorded, not only logged: the sweep resets its cursor and comes
            # round again, so without this a row whose bytes are gone is retried
            # hourly forever (N22 H64). Written with `.update()` because the
            # in-memory row is mid-failure and must not be saved wholesale.
            Image.objects.filter(pk=image.pk).update(media_unreadable_at=timezone.now())
            logger.warning("Marker thumbnail generation failed for image %s: %s", image.pk, exc, exc_info=True)
    return written


#: Cache key for the exclusive pk cursor :func:`backfill_image_marker_thumbnails` walks.
_MARKER_THUMBNAIL_BACKFILL_CURSOR_KEY = "image-marker-thumbnail-backfill-cursor"
#: Same rationale as _THUMBNAIL_BACKFILL_CURSOR_TTL.
_MARKER_THUMBNAIL_BACKFILL_CURSOR_TTL = 7 * 24 * 60 * 60


@shared_task(queue=Queue.MAINTENANCE)
def backfill_image_marker_thumbnails(limit: int | None = None) -> int:
    """Enqueue a bounded batch of photos that still lack a map-marker thumbnail.

    The marker-thumbnail mirror of :func:`backfill_image_thumbnails` - see its docstring for the
    walk/cursor behaviour, which this copies exactly.

    Args:
        limit: Override the default batch size.

    Returns:
        How many image ids were queued (0 when the library is caught up, or this tick only reset the
        cursor).
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

    Re-enqueues keywording for every row it fixes, so a write that failed during upload still ends in
    keywords rather than needing a manual sweep.

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
        except (*OBJECT_STORE_ERRORS, OSError, ValueError, PILDecompressionBombError) as exc:
            if is_transient(exc):
                logger.warning("Analysis thumbnail generation stopped at image %s: storage failed: %s", image.pk, exc, exc_info=True)
                break
            # Recorded for the same reason as in generate_image_thumbnails: the sweep comes round again.
            Image.objects.filter(pk=image.pk).update(media_unreadable_at=timezone.now())
            logger.warning("Analysis thumbnail generation failed for image %s: %s", image.pk, exc, exc_info=True)
            continue
        fields = ["analysis_thumbnail", "updated"]
        if image.media_unreadable_at is not None:
            image.media_unreadable_at = None
            fields.append("media_unreadable_at")
        image.save(update_fields=fields)
        written += 1
        # Same gate process_image_upload applies - a profile-less row has no uploader who opted in, and
        # keywording it would spend a billed call nobody asked for.
        if image.profile is not None and image.profile.generate_photo_keywords:
            safely_enqueue_task(generate_image_keywords, image.pk)
    return written


#: Cache key for the exclusive pk cursor :func:`backfill_image_analysis_thumbnails` walks.
_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_KEY = "image-analysis-thumbnail-backfill-cursor"
#: Long enough that a beat outage does not restart a half-finished walk at pk 0.
_ANALYSIS_THUMBNAIL_BACKFILL_CURSOR_TTL = 7 * 24 * 60 * 60


@shared_task(queue=Queue.MAINTENANCE)
def backfill_image_analysis_thumbnails(limit: int | None = None) -> int:
    """Queue analysis-copy generation for photos that still lack one.

    The analysis-copy mirror of :func:`backfill_image_thumbnails` - see its docstring for the
    walk/cursor behaviour, which this copies exactly.

    Args:
        limit: Override the default batch size.

    Returns:
        How many image ids were queued (0 when the library is caught up, or this tick only reset the
        cursor).
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


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def generate_image_keywords(image_id: int) -> dict[str, int]:
    """Generate searchable keywords for an uploaded photo via keyword plugins.

    Enqueued at the end of ``process_image_upload`` (fully in the background - uploads never wait on
    it).

    Args:
        image_id: PK of the image to keyword.

    Returns:
        Mapping of provider slug to keywords stored.
    """
    from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image

    return generate_keywords_for_image(image_id)


#: Longer than a sweep's soft limit, so a run that overruns still holds off the next.
_KEYWORD_RETRY_SWEEP_LOCK_SECONDS = 660
_KEYWORD_RETRY_SWEEP_LOCK_KEY = "urbanlens:keyword-retry:sweep-lock"


@shared_task(soft_time_limit=600, time_limit=_KEYWORD_RETRY_SWEEP_LOCK_SECONDS, queue=Queue.MAINTENANCE)
@external_background_task("keyword-retry-sweep")
def sweep_keyword_retries(limit: int | None = None) -> dict[str, int]:
    """Ask keyword sources again about the photos they did not answer for (P323).

    Only the source that did not answer is asked, a capped batch per run, never a source this environment does not
    call or one that is backing off; see ``services.photos.keyword_retry``.

    Args:
        limit: The most photos to ask about; the service's batch by default.

    Returns:
        How many retry rows came to each outcome.
    """
    from urbanlens.dashboard.services.core.locks import beat_lock
    from urbanlens.dashboard.services.photos import keyword_retry

    with beat_lock(_KEYWORD_RETRY_SWEEP_LOCK_KEY, _KEYWORD_RETRY_SWEEP_LOCK_SECONDS) as acquired:
        if not acquired:
            return {}
        return {str(outcome): count for outcome, count in keyword_retry.sweep(limit or keyword_retry.SWEEP_BATCH).items()}


@shared_task(queue=Queue.BULK)
def submit_redata_photos(image_ids: list[int]) -> bool:
    """Submit photo observations to REData and cache the confidence scores it returns.

    Best-effort like every other REData call site (e.g. ``import_immich_photos``) - a REData outage is
    logged and swallowed rather than retried, since a later submission (or the periodic photo itself
    being re-saved) will pick it up.

    Args:
        image_ids: PKs of photos to submit - filtered to actual photos (not video/document rows) with a
        resolved location.

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


@shared_task(queue=Queue.BULK)
def submit_redata_photo_vote(image_id: int, profile_id: int, is_relevant: bool) -> bool:
    """Submit one relevance vote on a photo to REData.

    Enqueued from ``services.photos.redata_relevance.queue_relevance_vote`` whenever a user marks a
    materialized photo relevant or not relevant.

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


@shared_task(queue=Queue.MAINTENANCE)
def sync_redata_label_definitions(profile_ids: list[int], definitions: list[dict]) -> bool:
    """Push tag/category label definitions into every listed profile's REData taxonomy.

    Enqueued from ``services.labels.redata_suggestions.queue_label_definition_sync``/
    ``queue_label_retirement`` whenever a tag/category label is created, edited, reparented, or retired.

    Args:
        profile_ids: PKs of profiles whose taxonomy should receive ``definitions`` (a global label maps
        to every profile; an owned label maps to just its one...
        definitions: Definition dicts built by ``services.labels.redata_suggestions._label_definition``.

    Returns:
        True when at least one profile was targeted.
    """
    from urbanlens.dashboard.services.labels.redata_suggestions import sync_label_definitions

    if not profile_ids or not definitions:
        return False
    sync_label_definitions(profile_ids, definitions)
    return True


@shared_task(queue=Queue.BULK)
def sync_redata_pin_assignment(pin_id: int) -> bool:
    """Push one pin's complete current tag/category label set to REData.

    Enqueued from ``services.labels.redata_suggestions.queue_pin_assignment_sync``, itself called from
    the ``Pin.labels`` ``m2m_changed`` signal - the single choke point that sees every pin-tagging call
    site.

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


@shared_task(queue=Queue.BULK)
def sync_redata_pin_assignments(pin_ids: list[int]) -> int:
    """Chunk-shaped sibling of :func:`sync_redata_pin_assignment`, for a bulk import's fan-out (P109).

    A label added to every pin in an import (a list category, a shared tag) fires the
    ``Pin.labels`` ``m2m_changed`` signal once per pin - queued here instead of one broker task each.

    Args:
        pin_ids: PKs of the pins whose label sets changed.

    Returns:
        How many pins were found and synced.
    """
    synced = 0
    for pin_id in pin_ids:
        try:
            if sync_redata_pin_assignment(pin_id):
                synced += 1
        except Exception:
            logger.exception("sync_redata_pin_assignments: pin %s failed", pin_id)
    return synced


@shared_task(bind=True, max_retries=5, queue=SANDBOX_QUEUE)
def scan_comment_image(self, comment_id: int) -> bool:
    """Background malware-scan a newly-uploaded pin/wiki comment image.

    A clamd connectivity hiccup retries with backoff instead of immediately treating the upload as
    rejected.

    Args:
        comment_id: PK of the ``Comment`` whose image should be scanned.

    Returns:
        True when the scan completed and found the image clean.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.services.media.upload_retry import COMMENT_IMAGE, stop_waiting

    comment = Comment.objects.filter(pk=comment_id, pending_scan=True).select_related("profile", "pin", "wiki__location").first()
    if comment is None or not comment.image:
        stop_waiting(COMMENT_IMAGE, comment_id)
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
    from urbanlens.dashboard.services.media.upload_retry import TRIP_COMMENT_IMAGE, stop_waiting

    comment = TripComment.objects.filter(pk=comment_id, pending_scan=True).select_related("author", "trip").first()
    if comment is None or not comment.image:
        stop_waiting(TRIP_COMMENT_IMAGE, comment_id)
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
    from django.core.files.base import ContentFile

    from urbanlens.dashboard.services.media import upload_retry
    from urbanlens.dashboard.services.security.malware_scan import MalwareScanUnavailableError, malware_error_for_upload

    target = f"{model._meta.label}.image"  # noqa: SLF001 - _meta is Django's public model API
    # Read before the scan, which reports any OSError reading its stream as the scanner being down.
    try:
        with comment.image.open("rb") as handle:
            upload = ContentFile(handle.read(), name=comment.image.name)
    except STORAGE_ERRORS as exc:
        return _comment_storage_failed(task, comment, target, exc)

    try:
        malware_error = malware_error_for_upload(upload)
    except MalwareScanUnavailableError as exc:
        if task.request.retries >= task.max_retries:
            logger.exception("Malware scan permanently unavailable for comment %s after %s retries", comment.pk, task.request.retries)
            upload_retry.stop_waiting(target, comment.pk)
            reject_comment_upload(comment, "Our antivirus scanner was unavailable and your photo could not be scanned.")
            return False
        raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc

    if malware_error:
        upload_retry.stop_waiting(target, comment.pk)
        reject_comment_upload(comment, malware_error)
        return False

    from urbanlens.dashboard.services.media.storage import get_downscale_policy
    from urbanlens.dashboard.services.media.stored_field import Reencoded, reencode_stored_field

    owner = getattr(comment, "profile", None) or getattr(comment, "author", None)
    max_dimension, convert_webp = get_downscale_policy(owner) if owner is not None else (None, True)
    # pending_scan clears in the update that swaps in the re-encoded file, so the upload as sent is never shown.
    try:
        outcome = reencode_stored_field(
            model.objects.all(),
            comment.pk,
            "image",
            comment.image.name,
            max_dimension=max_dimension,
            convert_webp=convert_webp,
            only_if={"pending_scan": True},
            also_set={"pending_scan": False},
        )
    except STORAGE_ERRORS as exc:
        return _comment_storage_failed(task, comment, target, exc)
    upload_retry.stop_waiting(target, comment.pk)
    if outcome is Reencoded.UNDECODABLE:
        reject_comment_upload(comment, "That photo couldn't be processed.")
        return False
    if outcome is Reencoded.REPLACED:
        upload_retry.record_storage_success()
    return outcome is Reencoded.REPLACED


def _comment_storage_failed(task, comment, target: str, exc: Exception) -> bool:
    """Retry a comment image storage failed on, then leave it pending, waiting for storage; reject it once its file is gone.

    Args:
        task: The bound Celery task instance.
        comment: The pending ``Comment`` or ``TripComment``.
        target: Its :class:`~urbanlens.dashboard.models.upload_retry.UploadRetry` target.
        exc: What storage raised.

    Returns:
        False: the image was not published.

    Raises:
        Retry: While the task has retries left and the comment is not already waiting.
    """
    from urbanlens.dashboard.services.media import upload_retry

    if upload_retry.means_file_is_gone(exc):
        if upload_retry.file_is_gone(target, comment.pk, comment.image.name, exc):
            logger.warning("Rejecting comment %s: storage has not had its image for %s", comment.pk, upload_retry.GONE_GRACE)
            upload_retry.stop_waiting(target, comment.pk)
            reject_comment_upload(comment, "That photo couldn't be processed.")
        return False
    if task.request.retries < task.max_retries and not upload_retry.is_waiting(target, comment.pk):
        raise task.retry(exc=exc, countdown=min(60 * (2**task.request.retries), 900)) from exc
    logger.warning("Storage could not read or write the image for comment %s; it waits for storage", comment.pk, exc_info=exc)
    upload_retry.wait_for_storage(target, comment.pk, comment.image.name, exc)
    return False


def reject_comment_upload(comment, reason: str) -> None:
    """Notify a comment's author their upload was rejected, and remove the comment.

    The comment (text included) never went visible to anyone but its own author (see ``pending_scan``),
    so removing it outright and handing the author their own text back via the notification is simpler
    than leaving a permanently-broken "image rejected" placeholder behind - they can copy the text from
    the notification and try posting again.

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

    Prefers the Location of the pin or wiki the photo is attached to; otherwise falls back to
    matching/creating a Location at the photo's GPS coordinates.

    Args:
        image: The Image needing a location link.
        coords: (latitude, longitude) extracted from EXIF, or None.

    Returns:
        The resolved Location, or None when nothing places the photo.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.location.queryset import CoordinateOffTheGlobeError

    if image.pin is not None and image.pin.location_id is not None:
        return image.pin.location
    if image.wiki is not None and image.wiki.location_id is not None:
        return image.wiki.location
    if coords:
        lat, lng = coords
        try:
            location, _created = Location.objects.get_nearby_or_create(lat, lng)
        except CoordinateOffTheGlobeError:
            # A position stored before positions were checked.
            logger.warning("Image %s has a position off the globe; it is not placed", image.pk)
            return None
        return location
    return None


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, max_retries=None, queue=Queue.BULK)
def import_immich_photos(  # noqa: PLR0917 - a Celery task: safely_enqueue_task passes its arguments positionally, and queued messages carry that order
    self,
    pin_id: int,
    profile_id: int,
    asset_ids: list[str],
    visit_id_by_asset: dict[str, int] | None = None,
    done: dict[str, int] | None = None,
    storage_waits: int = 0,
) -> dict[str, int]:
    """Download selected Immich assets and import them onto a pin.

    An asset already imported to this pin, or one that would exceed the uploader's storage quota, is skipped rather
    than failing the whole batch. One that storage refuses is retried later with the assets after it
    (``library_import.PhotoImport``).

    Args:
        pin_id: PK of the pin to import onto.
        profile_id: PK of the requesting profile (also the pin owner).
        asset_ids: Immich asset ids selected in the picker dialog, or those left after a storage wait.
        visit_id_by_asset: When importing on behalf of an accepted ``PinSuggestion`` (see
            ``services.pins.pin_suggestions.accept_pin_suggestion``), maps an asset id to the visit its photo joins.
        done: Counts from the runs before a storage wait.
        storage_waits: Storage waits in a row so far.

    Returns:
        Counts of imported/skipped/failed/storage_unavailable assets, surfaced to the polling UI.
    """
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.photos.library_import import ImmichPhotoImport, ImportCounts

    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = ImmichAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Immich connection no longer exists.")
        return ImportCounts.from_dict(done).as_dict()
    return ImmichPhotoImport(self, profile, pin, account, visit_id_by_asset, done=done, storage_waits=storage_waits).run(asset_ids)


#: Coordinate precision the library sweep groups assets by, about 11m. Chosen to
#: sit well inside ``CLUSTER_RADIUS_M`` (50m): collapsing points that clustering
#: would merge anyway cannot change which cluster they land in.
_SWEEP_BUCKET_DECIMALS = 4


class _SweptPlace:
    """Assets from one place in a library sweep, held as counts rather than rows.

    A library has far fewer places in it than photos, so grouping as the sweep
    reads means peak memory tracks places. Nothing is discarded that anything
    downstream reads: ``weight`` carries how many photos the place stands for and
    ``extra_dates`` carries their date spread, which is what ``_dates_from_hits``
    and every ``hit_count`` derivation actually consume.
    """

    __slots__ = ("_dates", "_label", "_latitude", "_longitude", "_samples", "_taken_at", "_total")

    def __init__(self, latitude: float, longitude: float, taken_at: datetime, label: str | None) -> None:
        self._latitude = latitude
        self._longitude = longitude
        self._taken_at = taken_at
        self._label = label
        self._total = 0
        self._dates: set[str] = set()
        self._samples: list[str] = []

    def add(self, asset, sample_limit: int) -> None:
        """Fold one asset into this place.

        Args:
            asset: The ``SearchAsset`` being swept.
            sample_limit: How many asset ids to keep for review-queue thumbnails.
        """
        self._total += 1
        self._dates.add(asset.taken_at.date().isoformat())
        if len(self._samples) < sample_limit and asset.id:
            self._samples.append(asset.id)
        if not self._label and asset.city:
            self._label = asset.city

    def to_hits(self) -> list[LocationHit]:
        """One hit per kept sample, carrying this place's whole weight between them.

        Returns:
            Hits whose ``weight`` sums to every asset folded in here.
        """
        from urbanlens.dashboard.services.pins.pin_suggestions import LocationHit

        samples: list[str | None] = list(self._samples) or [None]
        extra = tuple(sorted(self._dates - {self._taken_at.date().isoformat()}))
        # The first hit carries the unattributed remainder and the full date
        # spread; the rest exist only to keep their sample asset ids.
        first = LocationHit(
            latitude=self._latitude,
            longitude=self._longitude,
            taken_at=self._taken_at,
            label=self._label,
            asset_id=samples[0],
            weight=self._total - (len(samples) - 1),
            extra_dates=extra,
        )
        rest = [LocationHit(latitude=self._latitude, longitude=self._longitude, taken_at=self._taken_at, label=self._label, asset_id=sample) for sample in samples[1:]]
        return [first, *rest]


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def sweep_immich_library_locations(self, profile_id: int) -> dict[str, int]:
    """Sweep a user's entire Immich library for places they've been.

    Only triggered by an explicit "Scan your library" action (see
    ``controllers.immich.ImmichLibraryScanStartView``), never on connect.

    Args:
        profile_id: PK of the requesting profile (also the Immich account owner).

    Returns:
        Summary counts (matched/new-pin suggestions touched, assets scanned).
    """
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.pin_suggestions.model import MAX_SUGGESTION_PHOTOS, PinSuggestionOrigin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.immich import ImmichGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.pins.pin_suggestions import ingest_location_hits
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

    # Collapsed per place as they arrive rather than kept per photo. LocationHit.weight
    # exists for exactly this and its docstring records the same fix on the local-scan
    # path; this sweep was the one that never got it, so peak memory tracked the size of
    # someone's library instead of the number of places in it.
    buckets: dict[tuple[float, float], _SweptPlace] = {}
    scanned = 0
    try:
        for page, _page_total in gateway.iter_library_assets():
            for asset in page:
                scanned += 1
                if asset.lat is None or asset.lon is None or asset.taken_at is None:
                    continue
                key = (round(asset.lat, _SWEEP_BUCKET_DECIMALS), round(asset.lon, _SWEEP_BUCKET_DECIMALS))
                place = buckets.get(key)
                if place is None:
                    place = buckets[key] = _SweptPlace(latitude=asset.lat, longitude=asset.lon, taken_at=asset.taken_at, label=asset.city)
                place.add(asset, sample_limit=MAX_SUGGESTION_PHOTOS)
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
    hits = [hit for place in buckets.values() for hit in place.to_hits()]
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


#: How many consecutive whole-batch REData request failures (network error, non-200, unparseable body - see
#: CidResolutionResult.request_failed) this task tolerates before giving up.
_MAX_CONSECUTIVE_REDATA_FAILURES = 5

#: How many consecutive retries may report the exact same pending cids with zero of them resolved before this
#: task gives up on them. A batch still making real progress (even one cid resolved per round) never trips this,
#: since the counter resets whenever the pending set shrinks.
_MAX_CONSECUTIVE_NO_PROGRESS_RETRIES = 5

#: How long a deferred-lookup batch keeps trying before it gives up and turns into PinImportFailure rows for the
#: user to resolve by hand. The counters above choose only *how far apart* retries are spaced; this deadline is
#: the only thing that ends the batch.
_DEFERRED_LOOKUP_DEADLINE = timedelta(days=2)

#: Seconds between retries, indexed by attempt number (the last entry repeats).
_DEFERRED_RETRY_SCHEDULE = (120, 120, 120, 300, 600, 1800, 3600, 7200, 14400, 21600)

#: Seconds a batch waits on Google's rate limit, whatever the attempt.
_GOOGLE_RATE_LIMIT_RETRY_SECONDS = 65

#: The most retries the deadline can hold. Each retry waits at least the shortest gap above, and a batch that keeps
#: resolving a cid or two resets its counters to that gap, so this many retries always outlast the deadline. The
#: deadline ends a batch first; this ends one whose ``started_at`` it cannot read.
_DEFERRED_MAX_RETRIES = math.ceil(_DEFERRED_LOOKUP_DEADLINE.total_seconds() / min(*_DEFERRED_RETRY_SCHEDULE, _GOOGLE_RATE_LIMIT_RETRY_SECONDS))


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
        started_at: ISO timestamp of the batch's first attempt, or None on that first attempt (also None
        for any batch missing this field, which gets a...

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
        # The only producer stamps an aware `timezone.now().isoformat()`, but a replayed or hand-enqueued
        # message can carry a naive one, and subtracting it raises TypeError rather than the ValueError caught
        # above - killing the task instead of retiring the batch.
        started = timezone.make_aware(started)
    return timezone.now() - started >= _DEFERRED_LOOKUP_DEADLINE


def _place_resolved_pins(result, deferred_lists: list[dict], *, profile, auto_tag: bool) -> tuple[int, int, int]:
    """Place every pin in ``deferred_lists`` whose cid this round actually resolved.

    Called on *every* round of ``resolve_deferred_pin_locations``, not only the final one - see that
    task's own docstring ("places whatever resolves now"): a round with pending cids must still place
    whatever did resolve, not wait for the batch to fully clear.

    - ``result.resolved`` - place the pin.
    - ``result.unresolvable`` - a terminal "no such location" answer; record a ``NO_LOCATION_FOUND``
      failure card so the user can fix it by hand.
    - anything else (still pending) - do nothing at all. A pending cid is unfinished work, not a
      failure, and writing a card for it here would put a transient stat...

    Args:
        result: The ``CidResolutionResult`` for this round.
        deferred_lists: The lists being imported, in iter_confirmed_import_events's shape.
        profile: The importing profile.
        auto_tag: Whether to enqueue AI category suggestion for newly-created pins.

    Returns:
        ``(created, exists, skipped)`` for this round only - each retry is a fresh task invocation, so
        these are per-round counts, not per-batch...
    """
    from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.location import Location
    from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailureReason
    from urbanlens.dashboard.services.apis.locations.google.maps import _create_pin_from_confirmed
    from urbanlens.dashboard.services.core.bulk_followup import batching_follow_on_work
    from urbanlens.dashboard.services.labels.style_suggestions import resolve_or_create_styled_label
    from urbanlens.dashboard.services.pins.pin_import_failures import auto_resolve_pin_import_failure_for_cid, record_pin_import_failure

    created_count = exists_count = skipped_count = 0
    # Coalesces this round's per-pin follow-on work (wiki creation, category suggestion, reputation
    # scoring) into bounded chunks rather than one broker task each - see confirmed_import.py's own
    # collector for the fast (non-deferred) path this mirrors (P109).
    with batching_follow_on_work():
        for lst in deferred_lists:
            stem = (lst.get("stem") or "").strip()
            list_label_ids = lst.get("label_ids") or []
            create_category = bool(lst.get("create_category", False))
            list_labels = list(Label.objects.pin_assignable_by(profile).filter(id__in=list_label_ids)) if list_label_ids else []

            category_label = None
            if create_category and stem:
                try:
                    category_label, _ = resolve_or_create_styled_label(profile, stem, KIND_CATEGORY)
                except CapacityExceededError as exc:
                    logger.info("Deferred import for profile %s: no category %r: %s", profile.pk, stem, exc)

            for pin_dict in lst.get("pins", []):
                cid = pin_dict["cid"]
                coords = result.resolved.get(cid)
                if coords is None:
                    if cid in result.rejected:
                        record_pin_import_failure(
                            profile,
                            cid,
                            name=pin_dict.get("name", ""),
                            description=pin_dict.get("description", ""),
                            maps_url=pin_dict.get("maps_url", "") or "",
                            reason=PinImportFailureReason.LOOKUP_ERROR,
                        )
                        skipped_count += 1
                    elif cid in result.unresolvable:
                        record_pin_import_failure(
                            profile,
                            cid,
                            name=pin_dict.get("name", ""),
                            description=pin_dict.get("description", ""),
                            reason=PinImportFailureReason.NO_LOCATION_FOUND,
                        )
                        skipped_count += 1
                    continue

                # Re-check now, not just at defer time: an earlier pin in this same batch referencing the same cid
                # (saved to two lists) may have just linked/created its Location.
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


def _with_confirmed_cids(deferred_lists: list[dict], profile_id: int) -> list[dict]:
    """*deferred_lists* with every pin's cid checked against its own Google Maps URL.

    The confirm step already does this; a retry queued before it did can still carry a cid that lost
    its low digits in the browser (REData P120), which no answer would ever be keyed by. A pin whose
    cid is refused outright is dropped, never looked up.
    """
    from urbanlens.dashboard.services.apis.locations.cid_validation import InvalidCidError
    from urbanlens.dashboard.services.apis.locations.google.maps import confirmed_pin_cid

    checked: list[dict] = []
    for lst in deferred_lists:
        pins = []
        for pin in lst.get("pins", []):
            try:
                cid = confirmed_pin_cid(pin)
            except InvalidCidError as exc:
                logger.warning("resolve_deferred_pin_locations: dropped a pin for profile %s whose cid was refused (%s)", profile_id, exc.code)
                continue
            if cid is not None:
                pins.append({**pin, "cid": cid})
        if pins:
            checked.append({**lst, "pins": pins})
    return checked


@shared_task(bind=True, max_retries=_DEFERRED_MAX_RETRIES, queue=Queue.BULK)
def resolve_deferred_pin_locations(  # noqa: PLR0917 - a Celery task: safely_enqueue_task passes its arguments positionally, and queued messages carry that order
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

    Queued by ``GoogleMapsGateway.iter_confirmed_import_events`` for any
    confirmed pin whose cid had neither an existing Location nor a cached
    Places lookup - see that method's docstring for why the preview's own
    lat/lng can't be trusted for these. Resolves every cid in one batch via
    ``services.apis.locations.cid_resolution.resolve_cids`` (REData if
    configured, else Google Places directly), places whatever resolves now,
    and retries later - with the resolved subset already placed and pruned
    from the retry args, so a retry never redoes finished work - if anything
    is still pending on a rate limit or a REData outage. Reports a summary via
    NotificationLog once every cid is either placed or confirmed unresolvable.

    Retries stop at :data:`_DEFERRED_LOOKUP_DEADLINE`, or at :data:`_DEFERRED_MAX_RETRIES` for a batch the deadline
    cannot end; either way the cids still pending become ``PinImportFailure`` rows and the user is told.

    Args:
        profile_id: PK of the importing profile.
        deferred_lists: Same shape as iter_confirmed_import_events's
            confirmed_lists, restricted to pins needing a live cid lookup -
            shrinks on each retry to just what's still unresolved.
        auto_tag: Whether to enqueue AI category suggestion for newly-created pins.
        original_total: Total pin count across the *first* call, before any retry narrowed
        deferred_lists - carried through retries purely so progress reporting...
        consecutive_request_failures: How many retries in a row have hit a whole-batch REData request
        failure with zero progress - see ``_MAX_CONSECUTIVE_REDATA_FAILURES``.
        consecutive_no_progress: How many retries in a row have come back with the exact same cids
        pending and none newly resolved - see...

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

    deferred_lists = _with_confirmed_cids(deferred_lists, profile_id)
    all_cids = [pin["cid"] for lst in deferred_lists for pin in lst.get("pins", [])]
    if not all_cids:
        return empty
    pin_dict_by_cid = {pin["cid"]: pin for lst in deferred_lists for pin in lst.get("pins", [])}
    # A cid whose pin dict still carries the Google Maps URL it was parsed from (e.g. a Takeout CSV import) -
    # passed through to REData, which resolves via a place's own URL faster and more reliably than the bare cid
    # alone.
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
    # self.request.retries counts this run's predecessors; Celery refuses a retry() past max_retries.
    out_of_time = _deferred_deadline_passed(started_at) or self.request.retries >= _DEFERRED_MAX_RETRIES
    if out_of_time and consecutive_request_failures:
        logger.error(
            "resolve_deferred_pin_locations: REData failed %d consecutive attempts resolving %d cid(s) for profile %s after %d retries - giving up.",
            consecutive_request_failures,
            len(all_cids),
            profile_id,
            self.request.retries,
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
            # Already tracked by consecutive_request_failures above - a failed request trivially leaves every
            # cid pending, which isn't the "REData responded but nothing moved" case this counter targets.
            consecutive_no_progress = 0
        else:
            consecutive_no_progress = consecutive_no_progress + 1 if len(result.pending) == len(all_cids) else 0

        if out_of_time:
            logger.error(
                "resolve_deferred_pin_locations: %d cid(s) for profile %s still pending after %d retries (%d without progress) - giving up.",
                len(all_cids),
                profile_id,
                self.request.retries,
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

        # Place whatever DID resolve this round before scheduling the retry: `remaining_pins` below drops every
        # resolved cid from the retry args, so any coordinate not placed here is never placed at all - not this
        # round, not a later one.
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
            countdown, message = _GOOGLE_RATE_LIMIT_RETRY_SECONDS, "Waiting on Google's rate limit - resuming shortly..."
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
        # throw=False: still waiting on REData is the expected, routine case, not an error - raising here would
        # go through Celery's task_retry signal (see UrbanLens/celery.py), which logs a WARNING + full traceback
        # on every single retry.
        self.retry(
            args=[profile_id, remaining_lists, auto_tag, total, consecutive_request_failures, consecutive_no_progress, started_at or timezone.now().isoformat()],
            countdown=countdown,
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


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, max_retries=None, queue=Queue.BULK)
def import_flickr_photos(self, pin_id: int, profile_id: int, photo_ids: list[str], done: dict[str, int] | None = None, storage_waits: int = 0) -> dict[str, int]:
    """Download selected Flickr photos and import them onto a pin.

    The same pipeline as ``import_immich_photos`` (``library_import.PhotoImport``); only the download source differs.

    Args:
        pin_id: PK of the pin to import onto.
        profile_id: PK of the requesting profile (also the pin owner).
        photo_ids: Flickr photo ids selected in the picker dialog, or those left after a storage wait.
        done: Counts from the runs before a storage wait.
        storage_waits: Storage waits in a row so far.

    Returns:
        Counts of imported/skipped/failed/storage_unavailable photos, surfaced to the polling UI.
    """
    from urbanlens.dashboard.models.flickr.model import FlickrAccount
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.photos.library_import import FlickrPhotoImport, ImportCounts

    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = FlickrAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Flickr connection no longer exists.")
        return ImportCounts.from_dict(done).as_dict()
    return FlickrPhotoImport(self, profile, pin, account, done=done, storage_waits=storage_waits).run(photo_ids)


@shared_task(bind=True, queue=Queue.INTERACTIVE)
def import_calendar_events(self, profile_id: int, selections: list[dict[str, Any]]) -> dict[str, Any]:
    """Create trips from the calendar events a profile picked in the import dialog.

    Args:
        profile_id: The importing profile.
        selections: Per-event choices, as ``services.trips.calendar_sync.import_events_as_trips`` takes them.

    Returns:
        ``{"level", "message", "created"}`` for the polling dialog's toast.
    """
    from urbanlens.dashboard.services.trips.calendar_sync import run_calendar_import

    def report(done: int, total: int) -> None:
        update_task_progress(self, current=done, total=total, message=f"Importing event {done} of {total}...")

    return run_calendar_import(profile_id, selections, report_progress=report)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, max_retries=None, queue=Queue.BULK)
def import_flickr_album_photos(  # noqa: PLR0917 - a Celery task: safely_enqueue_task passes its arguments positionally, and queued messages carry that order
    self,
    target_kind: str,
    target_id: int,
    profile_id: int,
    album_url: str,
    photo_ids: list[str],
    done: dict[str, int] | None = None,
    storage_waits: int = 0,
) -> dict[str, int]:
    """Download selected photos from a *public* Flickr album/photoset onto a pin or wiki.

    Unlike ``import_flickr_photos`` (one user's own OAuth-connected library), this imports from any public album
    given its URL - no OAuth token involved, just the site's Flickr API key.

    Args:
        target_kind: ``"pin"`` or ``"wiki"`` - which FK to set on the created ``Image`` rows.
        target_id: PK of the target pin or wiki.
        profile_id: PK of the requesting profile.
        album_url: The Flickr album URL as submitted in the lookup step - re-resolved here, on every run, rather than
            trusting a client-supplied photo list.
        photo_ids: Flickr photo ids selected in the preview grid, or those left after a storage wait.
        done: Counts from the runs before a storage wait.
        storage_waits: Storage waits in a row so far.

    Returns:
        Counts of imported/skipped/failed/storage_unavailable photos, surfaced to the polling UI.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.apis.flickr.public import FlickrPublicGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError
    from urbanlens.dashboard.services.photos.library_import import FlickrAlbumPhotoImport, ImportCounts

    counts = ImportCounts.from_dict(done).as_dict()
    profile = Profile.objects.filter(pk=profile_id).first()
    pin = Pin.objects.select_related("location").filter(pk=target_id).first() if target_kind == "pin" else None
    wiki = Wiki.objects.select_related("location").filter(pk=target_id).first() if target_kind == "wiki" else None
    location = pin.location if pin is not None else (wiki.location if wiki is not None else None)
    if profile is None or location is None or (pin is None and wiki is None):
        update_task_progress(self, current=0, total=1, message="Import failed: the pin, wiki, or your profile no longer exists.")
        return counts

    gateway = FlickrPublicGateway()
    try:
        album = gateway.get_album(album_url)
    except (ValueError, GatewayRequestError) as exc:
        update_task_progress(self, current=0, total=1, message=f"Import failed: {exc}")
        return counts

    listed = {photo.id for photo in album.photos}
    selected = [photo_id for photo_id in photo_ids if photo_id in listed]
    importer = FlickrAlbumPhotoImport(self, profile, pin=pin, wiki=wiki, location=location, album_url=album_url, album=album, gateway=gateway, done=done, storage_waits=storage_waits)
    return importer.run(selected)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, max_retries=None, queue=Queue.BULK)
def import_google_photos(  # noqa: PLR0917 - a Celery task: safely_enqueue_task passes its arguments positionally, and queued messages carry that order
    self,
    pin_id: int,
    profile_id: int,
    session_id: str,
    media_item_ids: list[str],
    done: dict[str, int] | None = None,
    storage_waits: int = 0,
) -> dict[str, int]:
    """Download selected Google Photos picker items and import them onto a pin.

    The same pipeline as ``import_immich_photos``/``import_flickr_photos`` (``library_import.PhotoImport``).

    Args:
        pin_id: PK of the pin to import onto.
        profile_id: PK of the requesting profile (also the pin owner).
        session_id: The picker session the items were selected in.
        media_item_ids: Picker API media item ids selected in the picker grid, or those left after a storage wait.
        done: Counts from the runs before a storage wait.
        storage_waits: Storage waits in a row so far.

    Returns:
        Counts of imported/skipped/failed/storage_unavailable items, surfaced to the polling UI.
    """
    from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.photos.library_import import GooglePhotosImport, ImportCounts

    pin = Pin.objects.select_related("location", "profile").filter(pk=pin_id).first()
    profile = Profile.objects.filter(pk=profile_id).first()
    account = GooglePhotosAccount.objects.get_for_profile(profile) if profile is not None else None
    if pin is None or profile is None or account is None:
        update_task_progress(self, current=0, total=1, message="Import failed: pin, profile, or Google Photos connection no longer exists.")
        return ImportCounts.from_dict(done).as_dict()
    importer = GooglePhotosImport(self, profile, pin, account, session_id, media_item_ids, done=done, storage_waits=storage_waits)
    return importer.run(media_item_ids)


#: One database backup at a time, whichever entry point started it: the admin button, the
#: schedule, and an OSError retry of either all reach :func:`_run_database_backup`.
DATABASE_BACKUP_LOCK_KEY = "backup:database:running"


def _run_database_backup(task=None) -> bool:
    """Run database backup and retention cleanup using current site settings.

    Returns:
        Whether a backup was written; False when another backup already holds the lock.
    """
    from urbanlens.core.controllers.backups.db import BACKUP_TIMEOUT_SECONDS, DatabaseBackup
    from urbanlens.dashboard.models.site_settings import SiteSettings

    # Outlives pg_dump's own timeout, so a dump that is still running still holds it.
    token = acquire_lock(DATABASE_BACKUP_LOCK_KEY, BACKUP_TIMEOUT_SECONDS + 300)
    if token is None:
        logger.info("Database backup skipped: another backup is still running")
        if task is not None:
            update_task_progress(task, current=1, total=1, message="Another backup is already running")
        return False
    try:
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
    finally:
        release_lock(DATABASE_BACKUP_LOCK_KEY, token)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def run_database_backup(self) -> bool:
    """Run database backup and retention cleanup from a Celery worker."""
    return _run_database_backup(self)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
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
# firing; a soft time limit propagates out of run_enrichment_cycle so the
# task winds down cleanly mid-batch.
@shared_task(bind=True, soft_time_limit=2900, time_limit=3100, queue=Queue.MAINTENANCE)
@external_background_task("scheduled-location-enrichment")
def run_scheduled_enrichment(self) -> dict:
    """Run one background-enrichment cycle when site settings allow it.

    Fired hourly by Celery beat.

    Returns:
        The cycle summary dict (also cached for the site-admin page), or a skip marker when another run
        holds the single-flight lock.
    """
    from urbanlens.dashboard.services.core.task_limits import SOFT_TIME_LIMIT_ERRORS
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
    except SOFT_TIME_LIMIT_ERRORS:
        logger.warning("run_scheduled_enrichment: cycle wound down at the soft time limit")
        return {"skipped": "timed_out"}
    finally:
        release_lock(RUN_LOCK_CACHE_KEY, _lock_token)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def refresh_pin_web_search(self, pin_id: int) -> int:
    """Pre-warm the web-search cache a pin's page reads: the shared search, and its own names' when it has any.

    Args:
        pin_id: PK of the pin; its names are read now, not when the task was queued.

    Returns:
        How many results the searches made here found; searches already cached are not made again.
    """
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
    from urbanlens.dashboard.services.search.pin_web_search import annotate_results, cached_web_searches, pin_web_searches, store_web_search
    from urbanlens.dashboard.services.search.search import search_web

    pin = Pin.objects.filter(pk=pin_id).select_related("location").first()
    if pin is None or pin.location is None:
        return 0
    searches = pin_web_searches(pin)
    cached = cached_web_searches(pin.location, searches)
    missing = [search for search in searches if search.scope.audience not in cached]
    found = 0
    for done, search in enumerate(missing):
        update_task_progress(self, current=done, total=len(missing), message="Refreshing web search...")
        try:
            results = annotate_results(search_web(search.query))
        except LocationContextUnavailableError as exc:
            logger.warning("refresh_pin_web_search: search unavailable for pin %s, leaving it to the page: %s", pin_id, exc)
            continue
        store_web_search(pin.location, search, results)
        found += len(results)
    if missing:
        update_task_progress(self, current=len(missing), total=len(missing), message="Web search refreshed")
    return found


# These safety check-in beat tasks share the RUN_LOCK_CACHE_KEY-style guard already used by
# run_scheduled_enrichment: they run every 5 minutes (see CELERY_BEAT_SCHEDULE), and without a lock, an
# overrunning execution (many due checkins, slow SMTP) racing the next scheduled tick could process the same
# rows twice - most seriously for escalation, which would otherwise re-email emergency contacts.
_CHECKIN_REMINDER_LOCK_CACHE_KEY = "urbanlens:safety:reminder-lock"
_CHECKIN_FINAL_WARNING_LOCK_CACHE_KEY = "urbanlens:safety:final-warning-lock"
_CHECKIN_ESCALATION_LOCK_CACHE_KEY = "urbanlens:safety:escalation-lock"
_CHECKIN_ARCHIVAL_SWEEP_LOCK_CACHE_KEY = "urbanlens:safety:archival-sweep-lock"
_CHECKIN_LOCK_TIMEOUT_SECONDS = 270  # just under the 5-minute beat interval


@shared_task(soft_time_limit=_CHECKIN_SOFT_TIME_LIMIT_SECONDS, time_limit=_CHECKIN_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def send_due_checkin_reminders() -> int:
    """Send the check-in-due reminder for every safety check-in whose time has arrived."""

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import send_checkin_reminder

    _lock_token = acquire_lock(_CHECKIN_REMINDER_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("send_due_checkin_reminders: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.due_for_reminder():
            # Isolated per check-in for the same reason the archival sweep below is: this queryset has a
            # deterministic ordering, so one repeatably-failing row would otherwise abort the run at the same
            # position on every tick and silently starve every check-in behind it.
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


@shared_task(soft_time_limit=_CHECKIN_SOFT_TIME_LIMIT_SECONDS, time_limit=_CHECKIN_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def send_final_checkin_warnings() -> int:
    """Send a final "check in now" warning for every safety check-in about to escalate."""

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


@shared_task(soft_time_limit=_CHECKIN_SOFT_TIME_LIMIT_SECONDS, time_limit=_CHECKIN_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def escalate_overdue_checkins() -> int:
    """Notify emergency contacts for every safety check-in whose grace period has elapsed."""

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import escalate_checkin

    _lock_token = acquire_lock(_CHECKIN_ESCALATION_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("escalate_overdue_checkins: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.overdue():
            # The most consequential of the three sweeps to isolate: this is the call that reaches someone's
            # emergency contacts, and escalate_checkin is already per-contact idempotent, so retrying a failed
            # one next tick only reaches the contacts the failed attempt never got to.
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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def archive_safety_checkin(checkin_id: int) -> None:
    """Encrypt-and-scrub one resolved check-in, dispatched with a countdown= at resolution time
    (``services.visits.safety.schedule_checkin_archival``) for responsiveness.

    Idempotent - ``services.visits.safety.archive_checkin`` no-ops if the check-in already has an
    archive, so a duplicate dispatch (or this task racing the sweep below) is harmless.
    """
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import archive_checkin

    checkin = SafetyCheckin.objects.filter(pk=checkin_id).first()
    if checkin is not None:
        archive_checkin(checkin)


@shared_task(soft_time_limit=_CHECKIN_SOFT_TIME_LIMIT_SECONDS, time_limit=_CHECKIN_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_due_safety_checkin_archival() -> int:
    """Backstop for ``archive_safety_checkin``'s countdown-scheduled dispatch.

    A broker/worker restart can drop a countdown-scheduled task outright; a bare 5-minute poll alone
    would also make the "no other viewers - archive immediately" case visibly wait up to 5 minutes,
    which isn't "immediately".
    """

    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.services.visits.safety import archive_checkin

    _lock_token = acquire_lock(_CHECKIN_ARCHIVAL_SWEEP_LOCK_CACHE_KEY, _CHECKIN_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("sweep_due_safety_checkin_archival: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for checkin in SafetyCheckin.objects.due_for_archival():
            # One checkin's failure (e.g. a malformed key bundle) must not stop the sweep from archiving every
            # other overdue checkin in this same run - each is independent, and the next sweep will retry only
            # the failed one.
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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def delete_expired_safety_checkins() -> int:
    """Permanently delete every resolved safety check-in past its owner's auto-delete window."""
    from urbanlens.dashboard.models.safety.model import SafetyCheckin

    due = SafetyCheckin.objects.due_for_auto_delete()
    count = due.count()
    due.delete()
    if count:
        logger.info("Auto-deleted %s expired safety check-in(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def prune_expired_undo_actions() -> int:
    """Delete UndoAction rows past their retention window.

    Each row's restore payload is stored directly on the row itself (see ``models.undo.UndoAction``'s
    docstring), not in a cache, specifically so an entry's restorability depends only on its own
    ``created`` timestamp versus ``UNDO_RETENTION`` - not on a separately-expiring cache TTL.
    """
    from urbanlens.dashboard.models.undo import UndoAction

    expired = UndoAction.objects.expired()
    count = expired.count()
    expired.delete()
    if count:
        logger.info("Pruned %s expired undo action(s)", count)
    return count


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def detect_dm_address_mentions(message_id: int) -> int:
    """Detect street addresses in a direct message's text and record their shares.

    The forward-geocoding half of DM location detection (see
    ``services.messaging.dm_location_detection``) - coordinates are detected inline at send time, but
    addresses need a geocoding API call, which never belongs in the request path.

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def hard_delete_expired_direct_messages(batch_size: int = 2000, max_per_run: int = 50000) -> int:
    """Permanently delete every direct message past its sender's disappearing-message window.

    In steady state a single hourly run has one hour of expiries to clear and takes one batch, but a
    backlog becoming due at once - the first run after a retention-policy change, or after the beat
    worker was down - would otherwise pull every due id into memory and send it back as a single ``IN
    (...)`` list, against Postgres' parameter and planning limits.

    Args:
        batch_size: Rows to claim per iteration.
        max_per_run: Ceiling on one invocation, so a large backlog drains over several scheduled runs
        instead of running unboundedly long.

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


#: Overlap lock for the deletion-reminder sweep, matching the safety reminder tasks above.
_DELETION_REMINDER_LOCK_CACHE_KEY = "urbanlens:account:deletion-reminder-lock"
_DELETION_REMINDER_LOCK_TIMEOUT_SECONDS = 3300  # just under the hourly beat interval

#: The hard-delete sweep has the same hazard for the same reason: it selects on `deletion_requested_at`, which
#: `hard_delete_profile` does not clear until it has already sent the final "your account has been deleted"
#: email.
_HARD_DELETE_LOCK_CACHE_KEY = "urbanlens:account:hard-delete-lock"
_HARD_DELETE_LOCK_TIMEOUT_SECONDS = 3300  # just under the hourly beat interval


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
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
            try:
                count += send_deletion_reminder(profile)
            except Exception:
                logger.exception("send_account_deletion_reminders: reminder for profile %s failed", profile.pk)
        if count:
            logger.info("Sent %s account deletion reminder(s)", count)
        return count
    finally:
        release_lock(_DELETION_REMINDER_LOCK_CACHE_KEY, _lock_token)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def hard_delete_expired_accounts() -> int:
    """Permanently delete every account whose 7-day deletion grace period has elapsed."""
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile

    _lock_token = acquire_lock(_HARD_DELETE_LOCK_CACHE_KEY, _HARD_DELETE_LOCK_TIMEOUT_SECONDS)
    if _lock_token is None:
        logger.info("hard_delete_expired_accounts: a previous run is still in flight; skipping")
        return 0
    try:
        count = 0
        for profile in Profile.objects.due_for_hard_delete():
            # One account that cannot be deleted must not hold up the rest; the next run retries it.
            try:
                hard_delete_profile(profile)
            except Exception:
                logger.exception("hard_delete_expired_accounts: deleting profile %s failed", profile.pk)
                continue
            count += 1
        if count:
            logger.info("Hard-deleted %s expired account(s)", count)
        return count
    finally:
        release_lock(_HARD_DELETE_LOCK_CACHE_KEY, _lock_token)


# No autoretry here, deliberately: run_panel_fetch owns the failure policy
# (suppression markers with their own TTLs), and Celery-level retries would
# race the poll-driven re-scheduling in schedule_panel_fetch. The time limits
# sit under external_data.FLIGHT_TTL_SECONDS so a hard-killed task's
# single-flight marker expires right after the task does.
@shared_task(soft_time_limit=110, time_limit=130, queue=Queue.INTERACTIVE)
def fetch_panel_source(source_key: str, pin_id: int, flight_token: str | None = None) -> None:
    """Fetch one external-data panel's upstream data in the background.

    Scheduled by ``external_data.schedule_panel_fetch`` when a Private Pin page finds a panel's store
    empty; the page polls until this task persists the result (LocationCache row, Boundary geometry
    column, or warmed slide caches).

    Args:
        source_key: An ``external_data.panel_sources()`` key.
        pin_id: PK of the pin whose panel data should be fetched.
        flight_token: Single-flight token from ``schedule_panel_fetch``; the fetch releases the marker
        only while it is still its own.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import run_panel_fetch

    pin = Pin.objects.select_related("location").filter(pk=pin_id).first()
    if pin is None:
        logger.info("fetch_panel_source: pin %s no longer exists", pin_id)
        return
    run_panel_fetch(source_key, pin, flight_token)


@shared_task(queue=Queue.INTERACTIVE)
def deliver_friend_invitation(invitation_id: int, url: str, send_join_email: bool) -> None:
    """Deliver an email friend invitation after the request that made it, so its latency tells the inviter nothing.

    Args:
        invitation_id: PK of the FriendInvitation.
        url: Absolute URL of its response page.
        send_join_email: Whether the address may be emailed.
    """
    from urbanlens.dashboard.services.social.friend_invitations import deliver

    deliver(invitation_id, url, send_join_email=send_join_email)


@shared_task(queue=Queue.INTERACTIVE)
def deliver_visit_invite(participant_id: int, invitation_id: int) -> None:
    """Offer a tagged visit to the account proven to own the address, after the request that tagged it.

    Args:
        participant_id: PK of the ExternalVisitParticipant.
        invitation_id: PK of the FriendInvitation issued for the same address, which holds it.
    """
    from urbanlens.dashboard.services.visits.visit_invites import deliver_to_participant

    deliver_to_participant(participant_id, invitation_id)


@shared_task(queue=Queue.INTERACTIVE)
def deliver_trip_invitation(invitation_id: int, url: str) -> None:
    """Deliver a trip invitation after the request that created it, so its latency tells the inviter nothing.

    Args:
        invitation_id: PK of the invitation.
        url: Absolute URL of its response page.
    """
    from urbanlens.dashboard.models.trips.invitation import TripInvitation
    from urbanlens.dashboard.services.trips.trip_invitations import deliver_invitation

    invitation = TripInvitation.objects.filter(pk=invitation_id).select_related("trip", "inviter__user").first()
    if invitation is not None:
        deliver_invitation(invitation, url)


@shared_task(queue=Queue.INTERACTIVE)
def send_email_task(to: str, subject: str, text_body: str, html_body: str) -> None:
    """Send an email queued by ``notification_delivery.queue_email``.

    Args:
        to: Recipient address.
        subject: Subject line.
        text_body: Plain-text body.
        html_body: HTML alternative, or empty.
    """
    from urbanlens.dashboard.services.notifications.notification_delivery import send_email_now

    send_email_now(to=to, subject=subject, text_body=text_body, html_body=html_body)


@shared_task(queue=Queue.INTERACTIVE)
def send_notification_email_task(profile_id: int, title: str, body_text: str, url: str | None, action_label: str) -> None:
    """Send a notification email queued by ``notification_delivery.send_notification_email``.

    Args:
        profile_id: PK of the recipient profile.
        title: Subject line and heading.
        body_text: Notification message text.
        url: Site-relative action link, or None.
        action_label: Button text.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.notifications.notification_delivery import send_notification_email_now

    recipient = Profile.objects.select_related("user").filter(pk=profile_id).first()
    if recipient is None:
        return
    send_notification_email_now(recipient, title=title, body_text=body_text, url=url, action_label=action_label)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def send_direct_message_email_if_unread(message_id: int) -> None:
    """Send the delayed "new message" email, unless it's since been read or already sent.

    Scheduled by ``services.messaging.direct_messages._schedule_message_email`` with a countdown, giving
    a logged-in recipient a chance to read the message organically first.

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
    # "Still unread" is not "still there".
    if message.tombstone_text_for(message.recipient_id) is not None:
        return
    # Re-asked, not remembered: sending was permitted 120 seconds ago, and a block is most often placed in
    # exactly that window - right after the message that prompted it.
    if not can_direct_message(message.sender, message.recipient):
        return
    if is_email_debounced(message.sender_id, message.recipient_id):
        return
    send_message_email_now(message)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def send_direct_message_text_alerts_if_unread(message_id: int) -> None:
    """Send the delayed WhatsApp/SMS "new message" alert, unless read or already alerted.

    Scheduled by ``services.messaging.direct_messages._schedule_message_text_alerts`` with a countdown,
    mirroring the delayed-email flow: no-ops if the message was read in the meantime or an earlier
    message in the same unread streak already triggered an alert (``send_message_text_alerts_now`` sets
    that marker; viewing the conversation clears it).

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
    # "Still unread" is not "still there".
    if message.tombstone_text_for(message.recipient_id) is not None:
        return
    # Re-asked, not remembered: sending was permitted 120 seconds ago, and a block is most often placed in
    # exactly that window - right after the message that prompted it.
    if not can_direct_message(message.sender, message.recipient):
        return
    if is_text_alert_debounced(message.sender_id, message.recipient_id):
        return
    send_message_text_alerts_now(message)


@shared_task(queue=Queue.MAINTENANCE)
def prune_api_call_logs() -> int:
    """Delete ApiCallLog rows older than every consumer's longest window.

    The table is written on every external API call and, until this task existed, never trimmed -
    ``ApiCallLog.prune_older_than_days`` was documented as the way to trim it, but nothing ever called
    it, so the rate-limit COUNTs that run before each call scanned an ever-growing table.

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


@shared_task(queue=Queue.MAINTENANCE, soft_time_limit=50, time_limit=55)
def roll_up_api_call_tallies() -> int:
    """Write tallied services' calls into ``ApiCallLog`` (``services.core.call_tally``).

    Returns:
        Rows written.
    """
    from urbanlens.dashboard.services.core.call_tally import roll_up

    return roll_up()


@shared_task(queue=Queue.MAINTENANCE)
def evaluate_provider_health_task() -> dict[str, int]:
    """Judge every external provider on its recent calls, back off the ones refusing or failing, and alert.

    See ``services.core.provider_health``.

    Returns:
        How many providers were evaluated, moved, alerted on and recovered.
    """
    from urbanlens.dashboard.services.core.provider_health import evaluate_provider_health

    report = evaluate_provider_health()
    return {"evaluated": report.evaluated, "transitions": len(report.transitions), "alerted": len(report.alerted), "recovered": len(report.recovered), "skipped": int(report.skipped)}


_PUBLIC_MEDIA_SWEEP_LOCK_KEY = "urbanlens:public-media-sweep-lock"
#: Under the beat interval (15 minutes), and over a run's budget plus its last batch.
_PUBLIC_MEDIA_SWEEP_LOCK_TIMEOUT_SECONDS = 600
_PUBLIC_MEDIA_SWEEP_BUDGET_SECONDS = 120
_PUBLIC_MEDIA_SWEEP_BATCH = 100


@shared_task(queue=Queue.MAINTENANCE, soft_time_limit=_PUBLIC_MEDIA_SWEEP_LOCK_TIMEOUT_SECONDS - 60, time_limit=_PUBLIC_MEDIA_SWEEP_LOCK_TIMEOUT_SECONDS)
def sweep_public_media_cache() -> int:
    """Remove cached public-source results that no one is shown (``services.media.public_media_sweep``).

    Scheduled, so a ``subject_relevance.RULE_VERSION`` bump is swept on the next tick; a run out of time queues the
    next rather than waiting for one.

    Returns:
        How many results were removed.
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.public_media_sweep import sweep_public_media

    token = acquire_lock(_PUBLIC_MEDIA_SWEEP_LOCK_KEY, _PUBLIC_MEDIA_SWEEP_LOCK_TIMEOUT_SECONDS)
    if token is None:
        logger.info("sweep_public_media_cache: a previous run is still in flight; skipping")
        return 0
    try:
        report = sweep_public_media(batch_size=_PUBLIC_MEDIA_SWEEP_BATCH, budget_seconds=_PUBLIC_MEDIA_SWEEP_BUDGET_SECONDS)
    finally:
        release_lock(_PUBLIC_MEDIA_SWEEP_LOCK_KEY, token)
    if report.removed:
        logger.info("Public-media sweep removed %d result(s) from %d cache row(s)", report.removed, report.rows)
    if report.remaining:
        safely_enqueue_task(sweep_public_media_cache, durable=False)
    return report.removed


@shared_task(queue=Queue.MAINTENANCE)
def prune_expired_sessions() -> None:
    """Delete expired session rows; the database session backends never do it themselves."""
    from django.core.management import call_command

    call_command("clearsessions")


@shared_task(queue=Queue.MAINTENANCE)
def prune_read_notifications() -> int:
    """Delete read notifications past ``SiteSettings.notification_retention_days``.

    Returns:
        How many were deleted.
    """
    from urbanlens.dashboard.services.core import retention

    deleted = retention.prune_read_notifications()
    if deleted:
        logger.info("Pruned %d read notification(s)", deleted)
    return deleted


@shared_task(queue=Queue.MAINTENANCE)
def prune_pin_tombstones() -> int:
    """Remove pin-deletion tombstones older than the sync retention window.

    A client whose ``deleted_since`` predates that floor gets an HTTP 410 full-resync signal from
    ``pins/deleted/`` instead of a silently incomplete deletions feed, so pruning can never cause a
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


#: Hourly, so a run that overruns the hour would otherwise meet the next one on the same candidates.
PUBLIC_PIN_EVALUATION_LOCK_KEY = "public-pins:evaluate"
PUBLIC_PIN_EVALUATION_LOCK_TIMEOUT_SECONDS = 55 * 60


@shared_task(queue=Queue.MAINTENANCE)
def evaluate_public_pin_candidates() -> dict[str, int]:
    """Run the public-pin eligibility engine and settle open votes.

    Everything lives in ``services.pins.public_pins`` - this is only the beat entry point.

    Returns:
        Transition counters (opened/reopened/suspended/passed/rejected).
    """
    from urbanlens.dashboard.services.pins import public_pins

    with beat_lock(PUBLIC_PIN_EVALUATION_LOCK_KEY, PUBLIC_PIN_EVALUATION_LOCK_TIMEOUT_SECONDS) as acquired:
        if not acquired:
            logger.info("Public-pin evaluation skipped: the previous run is still going")
            return {}
        counters = public_pins.evaluate_public_pin_candidates()
    if any(counters.values()):
        logger.info("Public-pin evaluation: %s", counters)
    return counters


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def send_notification_text_alerts_if_unread(notification_id: int) -> None:
    """Send the delayed WhatsApp/SMS alert for a site notification, unless read or debounced.

    Scheduled by ``services.notifications.notification_text_alerts.schedule_notification_text_alerts``
    (via the ``notification_text_alerts`` post_save signal) with a countdown, mirroring the DM
    text-alert flow: no-ops when the notification was read in the meantime, when a same-type text
    recently went to this recipient, or when the recipient turned the toggles off after it was enqueued.

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


@shared_task(queue=Queue.INTERACTIVE)
def broadcast_channel_group_message(group: str, message: dict[str, Any]) -> None:
    """Deliver ``message`` to every channel in channel-layer group ``group``.

    Runs the actual ``async_to_sync(channel_layer.group_send)`` call here, on ``celery-worker``'s
    prefork pool, rather than inline in whatever gunicorn gevent greenlet handled the request that
    triggered it - see ``services.core.channel_broadcast`` and docs/PROBLEMS.md's gevent/asyncio entry
    for why calling into asyncio directly from a gevent-scheduled request can raise
    ``SynchronousOnlyOperation`` on a *different*, unrelated concurrent request.

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


@shared_task(queue=Queue.INTERACTIVE)
def broadcast_channel_group_messages(deliveries: list[tuple[str, dict[str, Any]]]) -> None:
    """Deliver many ``(group, message)`` pairs inside one event loop.

    The batched counterpart to :func:`broadcast_channel_group_message`, for a
    fan-out whose length is set by how many people are in a conversation. One
    loop and one channel-layer connection for the whole batch; a failure on one
    group is logged and the rest still go.

    Args:
        deliveries: ``(channel group name, event dict)`` pairs.
    """
    layer = get_channel_layer()
    if layer is None:
        return

    async def send_all() -> None:
        for group, message in deliveries:
            try:
                await layer.group_send(group, message)
            except Exception:
                logger.exception("Failed to broadcast to channel-layer group %s", group)

    async_to_sync(send_all)()


@shared_task(soft_time_limit=240, time_limit=270, queue=Queue.INTERACTIVE)
def run_link_extraction(extraction_id: int) -> None:
    """Execute one queued AI link-extraction run (fetch, AI call, apply, notify).

    No Celery autoretry: the run itself records every failure mode on the LinkExtraction row (and
    notifies the user either way), and each attempt consumes a fetch plus AI tokens - retrying
    automatically would silently multiply cost for a user-triggered, user-visible action they can simply
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


@shared_task(soft_time_limit=180, time_limit=210, queue=Queue.INTERACTIVE)
def classify_trivia_submission(question_id: int) -> None:
    """Classify one pending user-submitted Trivia question and record its verdict.

    No Celery autoretry: each attempt consumes an AI call, and this is a background action with no user
    waiting on it - if this task never runs (or the classifier can't reach AI right now), the question
    simply stays PENDING_REVIEW (silently excluded from rotation, see
    services.trivia.submission.classify_and_update), no different from any other transient Celery
    outage.

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


@shared_task(queue=Queue.MAINTENANCE)
@external_background_task("scheduled-trivia-generation")
def run_scheduled_trivia_generation() -> dict:
    """Generate AI trivia questions for a bounded batch of not-yet-processed wikis.

    Fired hourly by Celery beat, mirroring run_scheduled_enrichment's single-flight lock (a run that's
    still going when the next hour ticks over is left alone rather than started twice).

    Returns:
        The sweep summary dict, or a skip marker when another run holds the single-flight lock.
    """

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


@shared_task(queue=Queue.MAINTENANCE)
@external_background_task("scheduled-trivia-wiki-incorporation")
def run_scheduled_trivia_wiki_incorporation() -> dict:
    """Fold well-upvoted user-submitted Trivia questions into their location wikis.

    Fired hourly by Celery beat, mirroring run_scheduled_trivia_generation's single-flight lock (a run
    still going when the next hour ticks over is left alone rather than started twice).

    Returns:
        The sweep summary dict, or a skip marker when another run holds the single-flight lock.
    """

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


#: Slugs a pin gets when it is created before anything knows what it is. Listed explicitly so the sweep below is
#: an indexed lookup rather than a scan of every pin; ``Pin.slug_is_placeholder`` still has the final say on
#: each candidate.
_PLACEHOLDER_SLUGS = ("unnamed-location", "unnamed", "dropped-pin", "pin", "location", "place", "point", "marker", "unknown-location", "unknown")

#: How old a pin must be before this sweep will change its slug.
_RESLUG_MIN_AGE = timedelta(hours=1)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def upgrade_placeholder_pin_names(batch_size: int = 1000) -> int:
    """Clear a pin's stored placeholder name once its location has a meaningful one to fall back to.

    Once ingestion is guaranteed to never store a placeholder name this way, this task (and the gap it
    patches) should be removed - new pins never need it.

    TODO: This exists only to backfill legacy data from earlier ingestion versions that didn't leave
    ``Pin.name`` as None for an unnamed pin.

    Args:
        batch_size: Maximum number of pins to upgrade in one run, so a single invocation can't run
        unboundedly long; any remainder is picked up by the next...

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

    # Refreshed here rather than on save: the pins that need it were named long ago, and only a slug that still
    # reads as a placeholder is replaced.
    reslugged = 0
    for pin in Pin.objects.filter(slug__in=_PLACEHOLDER_SLUGS, created__lt=timezone.now() - _RESLUG_MIN_AGE).select_related("location")[:batch_size]:
        if pin.refresh_placeholder_slug():
            reslugged += 1
    if reslugged:
        logger.info("upgrade_placeholder_pin_names: replaced %s placeholder slug(s)", reslugged)

    return upgraded


@shared_task(queue=Queue.INTERACTIVE)
def dispatch_native_push(notification_id: int) -> int:
    """Deliver one notification to the recipient's registered native push devices.

    Enqueued by ``models.notifications.signals.enqueue_native_push`` on every ``NotificationLog``
    insert; exits immediately for the (common) profile with no registered devices.

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


@shared_task(queue=Queue.INTERACTIVE)
def dispatch_push_to_devices(device_ids: list[int], payload: dict) -> int:
    """Deliver a payload to one batch of a profile's devices, handed off by ``send_push_to_profile``.

    Args:
        device_ids: At most ``push.PUSH_BATCH_SIZE`` device primary keys.
        payload: JSON-serializable notification payload.

    Returns:
        Number of devices successfully delivered to.
    """
    from urbanlens.dashboard.services.notifications.push import send_push_to_devices

    return send_push_to_devices(device_ids, payload)


_SPOTGUESSR_STALL_SWEEP_LOCK_CACHE_KEY = "urbanlens:spotguessr:stall-sweep-lock"
_SPOTGUESSR_STALL_SWEEP_LOCK_TIMEOUT_SECONDS = 110  # just under the 2-minute beat interval


@shared_task(soft_time_limit=_STALL_SWEEP_SOFT_TIME_LIMIT_SECONDS, time_limit=_STALL_SWEEP_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_stalled_spotguessr_sessions() -> int:
    """Force-reveal any SpotGuessr round that's been open too long.

    The safety net for a multiplayer round that can otherwise stall forever: a round only completes once
    every joined participant has guessed (``services.spotguessr.session.submit_guess``), but a
    participant who simply closes their tab is invisible to that check - there's no disconnect signal
    wired into the game state (see the SpotGuessr audit's "multiplayer stall" finding).
    """
    from datetime import timedelta

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def prewarm_spotguessr_round(session_id: int, sequence_index: int) -> bool:
    """Pre-select a SpotGuessr session's next round, so it's ready the instant a player reaches it.

    Queued by ``services.spotguessr.session.get_or_create_round`` right after it creates the round
    *before* this one - by the time that round is guessed and revealed, this round's location (and, for
    Street View mode, its Google Maps imagery - see ``services.spotguessr.street_view``, whose result
    this warms via the same lat/lng cache key) is already picked and cached, so the round that actually
    gets created next is a cache hit instead of live selection (see ``services.spotguessr.prewarm``).

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def prewarm_spotguessr_solo_start(profile_id: int, mode: str, config_dict: dict) -> bool:
    """Pre-select a solo player's likely first round before they've even clicked "start".

    Keyed by a fingerprint of the exact config (see ``services.spotguessr.prewarm``), so it's simply
    never redeemed - not wrongly redeemed - if the player changes a setting before actually starting.

    Args:
        profile_id: The player who loaded the SpotGuessr overview page.
        mode: The guessed ``SpotGuessrMode`` they'll start.
        config_dict: A ``GameConfig.to_dict()`` snapshot of their guessed settings (unknown keys
        ignored, mirroring ``session.config_from_session``).

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


@shared_task(soft_time_limit=_STALL_SWEEP_SOFT_TIME_LIMIT_SECONDS, time_limit=_STALL_SWEEP_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_stalled_trivia_sessions() -> int:
    """Force-reveal any Trivia round that's been open too long.

    The safety net for a multiplayer round that can otherwise stall forever: a round only completes once
    every joined participant has answered (``services.trivia.session.submit_answer``), but a participant
    who simply closes their tab is invisible to that check - there's no disconnect signal wired into the
    game state.
    """
    from datetime import timedelta

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


@shared_task(soft_time_limit=_STALL_SWEEP_SOFT_TIME_LIMIT_SECONDS, time_limit=_STALL_SWEEP_TIME_LIMIT_SECONDS, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_stalled_consensus_sessions() -> int:
    """Force-resolve any Consensus round that's been open too long.

    Unlike SpotGuessr/Trivia, a Consensus round has *two* sub-phases that can each stall independently:
    answer-collection (mirrors ``sweep_stalled_spotguessr_sessions`` - force-reveals via
    ``force_reveal_round``) and, for a competitive round whose answers disagreed, the follow-on vote
    (force-tallies via ``force_resolve_vote``).
    """
    from datetime import timedelta

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def recompute_fact_confidence(fact_id: int) -> None:
    """Recompute one Fact's confidence/status/value from its accumulated evidence.

    Queued (never called inline) from every Facts evidence write site - see
    ``services.facts.evidence.record_evidence``.
    """
    from urbanlens.dashboard.services.facts.confidence import recompute

    recompute(fact_id)


#: A flagged fact left alone this long lost its queued recompute.
STALE_FACT_CONFIDENCE_AGE = timedelta(minutes=10)
STALE_FACT_CONFIDENCE_BATCH = 500


@shared_task(queue=Queue.MAINTENANCE)
def sweep_stale_fact_confidence() -> int:
    """Queue a recompute for every fact whose new evidence no recompute has read.

    Returns:
        How many recomputes were queued.
    """
    from urbanlens.dashboard.models.facts.model import Fact
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    cutoff = timezone.now() - STALE_FACT_CONFIDENCE_AGE
    stale = Fact.objects.filter(needs_recompute=True, updated__lt=cutoff).order_by("updated").values_list("pk", flat=True)[:STALE_FACT_CONFIDENCE_BATCH]
    queued = 0
    for fact_id in stale:
        # A refusal is found again by the next sweep.
        if safely_enqueue_task(recompute_fact_confidence, fact_id, durable=False) is not None:
            queued += 1
    if queued:
        logger.info("Queued %d fact confidence recompute(s) that never ran", queued)
    return queued


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def process_device_scan_upload(self, upload_id: int) -> bool:
    """Classify, wiki-match, and cluster one wireless device-scan upload.

    Runs on the bulk queue - up to 100,000 rows of real CPU-bound geometry
    work, sized by one account's upload, so it must not share a pool with
    anything a person is waiting on.

    Claims the upload by flipping PENDING -> PROCESSING, so a redelivered or
    duplicate task is a no-op. The work and the flip to PROCESSED commit
    together, so ``record_absence_report`` counts a physical report once even
    when a worker dies mid-run: its partial work rolls back, and
    :func:`requeue_stalled_device_scans` hands the still-PROCESSING upload to
    another worker.

    Args:
        upload_id: PK of the DeviceScanUpload to process.

    Returns:
        True when this call claimed the upload (whether processing succeeded or failed); False when it no
        longer exists or is not pending.
    """
    from django.db import transaction
    from django.db.models import F

    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload, ScanUploadStatus
    from urbanlens.dashboard.services.device_scan.pipeline import process_scan_upload

    claimed = DeviceScanUpload.objects.filter(pk=upload_id, status=ScanUploadStatus.PENDING).update(status=ScanUploadStatus.PROCESSING, claimed_at=timezone.now(), attempts=F("attempts") + 1)
    if not claimed:
        logger.info("process_device_scan_upload: upload %s no longer exists or is not pending", upload_id)
        return False

    upload = DeviceScanUpload.objects.select_related("profile").prefetch_related("entries__device", "entries__expected_marker").filter(pk=upload_id).first()
    if upload is None:
        return False

    update_task_progress(self, current=0, total=1, message="Processing device scan...")
    try:
        with transaction.atomic():
            process_scan_upload(upload)
            DeviceScanUpload.objects.filter(pk=upload_id, status=ScanUploadStatus.PROCESSING).update(status=ScanUploadStatus.PROCESSED)
    except Exception as exc:
        logger.exception("process_device_scan_upload: failed for upload %s", upload_id)
        DeviceScanUpload.objects.filter(pk=upload_id).update(status=ScanUploadStatus.FAILED, error=str(exc))
        update_task_progress(self, current=1, total=1, message="Device scan processing failed")
        return True

    update_task_progress(self, current=1, total=1, message="Device scan processed")
    return True


#: A pending upload older than this lost its enqueue.
STALLED_SCAN_PENDING_AGE = timedelta(minutes=15)


def stalled_scan_claim_age() -> timedelta:
    """How long a processing upload's claim is honoured: past the longest hard limit E013 allows on its queue."""
    from urbanlens.dashboard.services.core.task_limits import ceiling_for

    return timedelta(seconds=ceiling_for(process_device_scan_upload.queue) + 60)


#: Claims after which an upload that keeps killing its worker is marked failed.
MAX_SCAN_UPLOAD_ATTEMPTS = 3
STALLED_SCAN_BATCH = 200


@shared_task(queue=Queue.MAINTENANCE)
def requeue_stalled_device_scans() -> int:
    """Re-enqueue device-scan uploads nothing is processing.

    A pending upload whose enqueue was lost, or a processing one whose worker died (its work rolled back
    with it), goes back to pending and is queued again. One that has been claimed
    ``MAX_SCAN_UPLOAD_ATTEMPTS`` times is marked failed instead.

    Returns:
        How many uploads were queued.
    """
    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload, ScanUploadStatus
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    now = timezone.now()
    stalled = DeviceScanUpload.objects.stalled(pending_before=now - STALLED_SCAN_PENDING_AGE, claimed_before=now - stalled_scan_claim_age())
    queued = 0
    for upload_id, status, claimed_at, attempts in stalled.values_list("pk", "status", "claimed_at", "attempts")[:STALLED_SCAN_BATCH]:
        if status == ScanUploadStatus.PROCESSING:
            same_claim = DeviceScanUpload.objects.filter(pk=upload_id, status=ScanUploadStatus.PROCESSING, claimed_at=claimed_at)
            if attempts >= MAX_SCAN_UPLOAD_ATTEMPTS:
                same_claim.update(status=ScanUploadStatus.FAILED, error="Processing never finished after several attempts.")
                continue
            if not same_claim.update(status=ScanUploadStatus.PENDING):
                continue
        # The next sweep finds it again if the broker refuses this.
        if safely_enqueue_task(process_device_scan_upload, upload_id, durable=False) is not None:
            queued += 1
    if queued:
        logger.info("Re-enqueued %d stalled device-scan upload(s)", queued)
    return queued


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def evaluate_achievements_for_profile(profile_id: int, metric_keys: list[str] | None = None) -> int:
    """Grant any achievements a profile now qualifies for.

    Queued by ``models.achievements.signals`` after a contribution, but only when some active award
    actually measures the affected metric - so this runs rarely, and when it does it re-checks a single
    count rather than sweeping.

    Args:
        profile_id: PK of the profile that contributed.
        metric_keys: Registry keys of the metrics to re-check.

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def backfill_achievement(achievement_id: int) -> int:
    """Grant a newly defined achievement to everyone who already qualifies.

    Queued when an admin saves an ``Achievement`` or asks for a re-check, so awards added at any point reach
    users retroactively. Dispatch only: each profile range is its own :func:`backfill_achievement_range`.

    Args:
        achievement_id: PK of the achievement to backfill.

    Returns:
        How many range subtasks were queued.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement
    from urbanlens.dashboard.models.profile import Profile
    from urbanlens.dashboard.services.achievements.evaluate import BACKFILL_CHUNK_SIZE
    from urbanlens.dashboard.services.core.celery import dispatch_pk_ranges

    achievement = Achievement.objects.filter(pk=achievement_id).first()
    if achievement is None:
        logger.info("backfill_achievement: achievement %s no longer exists", achievement_id)
        return 0
    if not achievement.is_active:
        return 0

    return dispatch_pk_ranges(Profile.objects.all(), backfill_achievement_range, achievement_id, chunk_size=BACKFILL_CHUNK_SIZE)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def backfill_achievement_range(achievement_id: int, start_pk: int, end_pk: int) -> int:
    """Grant one achievement to the qualifying profiles with ``start_pk <= pk <= end_pk``.

    Args:
        achievement_id: PK of the achievement to backfill.
        start_pk: Lowest profile pk in the range, inclusive.
        end_pk: Highest profile pk in the range, inclusive.

    Returns:
        How many profiles received the award.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement
    from urbanlens.dashboard.services.achievements.evaluate import evaluate_achievement_in_range

    achievement = Achievement.objects.filter(pk=achievement_id).first()
    if achievement is None:
        return 0
    return evaluate_achievement_in_range(achievement, start_pk, end_pk)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def score_reputation_event(event_id: int) -> str:
    """Work out what one recorded contribution was worth.

    Deferred because establishing how badly a target needed a contribution means querying that target's
    state, which for photos can mean walking external gallery panels - by far the most expensive input
    in the model, and exactly the cost this feature must not add to a page load.

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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def score_reputation_events(event_ids: list[int]) -> int:
    """Chunk-shaped sibling of :func:`score_reputation_event`, for a bulk import's fan-out (P109).

    ``_decay_multiplier`` is explicitly order-independent (see
    ``services.reputation.scoring``), so scoring a chunk's events in any order is safe. Each
    touched profile's total is rebuilt once for the whole chunk rather than once per event -
    ``recompute_total`` fully rebuilds from the ledger every call, so recomputing it per event here
    would repeat the same O(n) read for every other event belonging to that profile in the chunk.

    Args:
        event_ids: PKs of the ledger rows to value.

    Returns:
        How many events were scored (excludes already-scored and missing rows).
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent
    from urbanlens.dashboard.services.reputation.scoring import recompute_total, score_event

    touched: set[int] = set()
    scored = 0
    for event in ReputationEvent.objects.filter(pk__in=event_ids, value__isnull=True):
        try:
            value = score_event(event)
        except Exception:
            logger.exception("score_reputation_events: event %s failed", event.pk)
            continue
        if value is not None:
            scored += 1
        touched.add(event.profile_id)

    for profile_id in touched:
        recompute_total(profile_id)
    return scored


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.BULK)
def recompute_reputation_total(profile_id: int) -> str:
    """Rebuild one profile's cached reputation totals from the ledger.

    Args:
        profile_id: Whose totals to rebuild.

    Returns:
        The new total as a string.
    """
    from urbanlens.dashboard.services.reputation.scoring import recompute_total

    return str(recompute_total(profile_id))


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_reputation(chunk_size: int = 500) -> int:
    """Drain unscored ledger rows and rebuild any totals known to be stale.

    Dispatch only: rows are sliced into bounded ranges, in pk order, and each range is handled by its
    own subtask, so a chunk that crashes costs its own range rather than the whole sweep.

    Args:
        chunk_size: Maximum rows per subtask.

    Returns:
        How many subtasks were dispatched.
    """
    from urbanlens.dashboard.models.reputation.model import ProfileReputation, ReputationEvent
    from urbanlens.dashboard.services.core.celery import dispatch_pk_ranges, safely_enqueue_task

    dispatched = dispatch_pk_ranges(ReputationEvent.objects.unscored(), sweep_reputation_range, chunk_size=chunk_size)

    for profile_id in ProfileReputation.objects.stale().values_list("profile_id", flat=True):
        if safely_enqueue_task(recompute_reputation_total, profile_id, durable=True) is not None:
            dispatched += 1

    return dispatched


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
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


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_achievements(chunk_size: int = 1000) -> int:
    """Fan the nightly achievement sweep out as bounded profile-range subtasks.

    Evaluating everything in one task would hit the hard ``CELERY_TASK_TIME_LIMIT`` at scale and die
    mid-iteration; a bounded chunk cannot approach the limit, and a chunk that crashes anyway costs only
    its own range until the next nightly dispatch.

    Args:
        chunk_size: Maximum profiles per subtask.

    Returns:
        How many range subtasks were enqueued.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement
    from urbanlens.dashboard.models.profile import Profile
    from urbanlens.dashboard.services.core.celery import dispatch_pk_ranges

    # Same gate the contribution signals apply: with no active award defined
    # there is provably nothing to evaluate, so don't fan out empty subtasks.
    if not Achievement.objects.active().exists():
        return 0

    return dispatch_pk_ranges(Profile.objects.all(), sweep_achievements_range, chunk_size=chunk_size)


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
def sweep_achievements_range(start_pk: int, end_pk: int) -> int:
    """Evaluate every achievement for profiles with ``start_pk <= pk <= end_pk``.

    The range is evaluated with one bulk metric pass for the whole chunk, so it costs on the order of
    the metric count in queries rather than ~30 per profile.

    Args:
        start_pk: Lowest profile pk in the chunk, inclusive.
        end_pk: Highest profile pk in the chunk, inclusive.

    Returns:
        Total awards granted across the chunk.
    """
    from urbanlens.dashboard.services.achievements.evaluate import evaluate_profiles_in_range

    return evaluate_profiles_in_range(start_pk, end_pk)


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
@external_background_task("stripe-subscriptions-sync")
def sync_stripe_subscriptions(self, starting_after: str | None = None, sweep_started_at: float | None = None) -> int:
    """Re-sync one page of Stripe subscriptions onto their RoleSubscription rows, then hand off the next page.

    Webhooks (see controllers.billing_webhooks.StripeWebhookView) are the primary mechanism for keeping
    RoleSubscription in sync - this is the nightly safety net for deliveries Stripe couldn't complete. Each page is
    its own task, so a retry repeats one page rather than the whole sweep. After the last page,
    ``reconcile_unlisted_stripe_subscriptions`` retrieves the live rows no page reached.

    Args:
        starting_after: The previous page's last subscription id; None starts a sweep.
        sweep_started_at: Unix time the sweep started; None starts a sweep.

    Returns:
        How many rows this page applied.
    """
    import stripe

    from urbanlens.dashboard.services.billing import stripe_client, sync
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    if not stripe_client.is_configured():
        return 0
    stripe_client.configure()
    if sweep_started_at is None:
        # Whole seconds, like the stamps the pages write, so a row this sweep applies never reads as older than it.
        sweep_started_at = float(int(timezone.now().timestamp()))

    try:
        progress = sync.sync_page(starting_after)
    except (stripe.APIConnectionError, stripe.RateLimitError, stripe.APIError) as exc:
        raise self.retry(exc=exc) from exc

    if progress.resume_after is not None:
        safely_enqueue_task(sync_stripe_subscriptions, progress.resume_after, sweep_started_at)
    else:
        safely_enqueue_task(reconcile_unlisted_stripe_subscriptions, sweep_started_at, 0)
    return progress.applied


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.MAINTENANCE)
@external_background_task("stripe-subscriptions-sync")
def reconcile_unlisted_stripe_subscriptions(sweep_started_at: float, after_pk: int, chunk_size: int = 100) -> int:
    """Retrieve one chunk of live RoleSubscription rows the Stripe listing did not reach, then hand off the next.

    Args:
        sweep_started_at: Unix time the sweep started.
        after_pk: Resume after this primary key.
        chunk_size: Rows per task.

    Returns:
        How many rows this chunk applied.
    """
    from datetime import UTC

    from urbanlens.dashboard.services.billing import stripe_client, sync
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    if not stripe_client.is_configured():
        return 0
    stripe_client.configure()

    progress = sync.reconcile_unlisted(datetime.fromtimestamp(sweep_started_at, tz=UTC), after_pk, chunk_size)
    if progress.resume_after is not None:
        safely_enqueue_task(reconcile_unlisted_stripe_subscriptions, sweep_started_at, progress.resume_after, chunk_size)
    return progress.applied


@shared_task(queue=Queue.MAINTENANCE)
def advance_pwyw_usage_ledgers() -> int:
    """Advance every pay-what-you-want RoleSubscription's usage ledger.

    invoice.payment_succeeded already ticks a subscription's ledger the moment a payment lands (see
    services.billing.banking), but that's the only trigger while a subscription is actively billed - a
    canceled subscription gets no further Stripe events at all, so this daily sweep is what keeps its
    banked balance counting down (and eventually running out) once the money stops coming in.

    Only ledgers that could move are visited (``RoleSubscriptionQuerySet.ledger_advance_due``); each is advanced under
    its own row lock, so a re-run is harmless.

    Returns:
        How many pay-what-you-want subscriptions were checked.
    """
    from urbanlens.dashboard.models.billing import RoleSubscription
    from urbanlens.dashboard.services.billing import banking

    due = RoleSubscription.objects.ledger_advance_due(timezone.now()).select_related("role").order_by("pk")
    count = 0
    last_pk = 0
    while chunk := list(due.filter(pk__gt=last_pk)[:500]):
        last_pk = chunk[-1].pk
        for role_subscription in chunk:
            # This daily sweep is the only thing counting a canceled subscription's banked
            # balance down, so one row failing must not freeze every other user's ledger.
            try:
                banking.advance_usage_ledger(role_subscription)
                count += 1
            except Exception:
                logger.exception("advance_pwyw_usage_ledgers: failed to advance subscription %s", role_subscription.pk)
    return count


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def cache_media_item_into_album(album_id: int, profile_id: int, source: str, url: str, page_url: str = "", caption: str = "") -> int | None:
    """Download an external media item and file the local copy into an album.

    The relevance vote is written synchronously by the request that queues this (it's a cheap DB write,
    and it's the part that must not be lost), so this task only owns the slow half: the HTTP download.

    Args:
        album_id: PK of the Album to file the photo into.
        profile_id: PK of the Profile the download is attributed to.
        source: Provider panel key (e.g. ``"wikimedia"``).
        url: The item's full-resolution image url.
        page_url: Optional provider page url, for attribution.
        caption: Optional caption carried from the gallery tile.

    Returns:
        PK of the materialized Image, or None if the album/profile vanished or the download failed.
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
    # A personal (Profile-owned) album has no Pin/Wiki to attach media to at all - checked directly rather than
    # via `getattr(owner, "location", None)`, which happened to also catch this case today only because Profile
    # has no `location` attribute of its own to shadow the default.
    if not isinstance(owner, Pin | Wiki):
        logger.info("cache_media_item_into_album: album %s has no pin or wiki to attach media to", album_id)
        return None
    location = owner.location
    if location is None:
        logger.info("cache_media_item_into_album: album %s has no location to attach media to", album_id)
        return None
    try:
        ensure_room(ALBUM_PHOTOS, album.pk)
    except CapacityExceededError as exc:
        logger.info("cache_media_item_into_album: album %s is full, not downloading %s: %s", album_id, url, exc)
        return None

    # isinstance rather than `album.parent_pin_id is not None`: it asks the question directly of the object
    # album_owner actually returned, so the two cannot disagree - and narrows each argument to exactly the type
    # materialize_media_item expects, rather than assuming "not a Pin" means "must be a Wiki" (album_owner can
    # also return a bare Profile).
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

    try:
        add_images_to_album(album, [image], profile)
    except CapacityExceededError as exc:
        logger.info("cache_media_item_into_album: album %s filled during the download; %s stays unfiled: %s", album_id, image.pk, exc)
    queue_relevance_vote(image, profile, is_relevant=True)
    return image.pk


@shared_task(soft_time_limit=240, time_limit=270, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def cache_media_item_into_wiki(wiki_id: int, profile_id: int, source: str, url: str, page_url: str = "", caption: str = "") -> int | None:
    """Download an external media item and attach the local copy to a wiki.

    Args:
        wiki_id: PK of the Wiki to attach the photo to.
        profile_id: PK of the Profile the download is attributed to.
        source: Provider panel key (e.g. ``"wikimedia"``).
        url: The item's full-resolution image url.
        page_url: Optional provider page url, for attribution.
        caption: Optional caption carried from the gallery tile.

    Returns:
        PK of the materialized Image, or None if the wiki/profile vanished or the download failed.
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


def _parse_iso_days(iso_days: list[str], what: object) -> list[date]:
    days = []
    for iso in iso_days:
        try:
            days.append(date.fromisoformat(iso))
        except ValueError:
            logger.warning("fetch_recorded_weather: ignoring malformed date %r for %s", iso, what)
    return days


@shared_task(soft_time_limit=180, time_limit=210, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def fetch_recorded_weather_at(latitude: float, longitude: float, iso_days: list[str]) -> int:
    """Store the recorded weather for a set of days at a coordinate.

    Queued by the pages that show recorded weather - visit history and a trip's weather panel - which read
    stored rows without fetching, so a slow REData never holds up their render.

    Args:
        latitude: WGS-84 latitude, usually a weather cell's centre.
        longitude: WGS-84 longitude.
        iso_days: ISO dates to fetch, as the caller found them missing.

    Returns:
        How many of the days are now stored, for the task log.
    """
    from urbanlens.dashboard.services.locations.visit_weather import recorded_days_at

    days = _parse_iso_days(iso_days, "a weather cell")
    if not days:
        return 0
    return len(recorded_days_at(latitude, longitude, days))


@shared_task(soft_time_limit=180, time_limit=210, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3}, queue=Queue.INTERACTIVE)
def fetch_recorded_weather(location_id: int, iso_days: list[str]) -> int:
    """:func:`fetch_recorded_weather_at` for a Location, which may have been deleted since it was queued.

    Args:
        location_id: PK of the Location the days belong to.
        iso_days: ISO dates to fetch.

    Returns:
        How many of the days are now stored.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.visit_weather import recorded_days

    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        logger.info("fetch_recorded_weather: location %s no longer exists", location_id)
        return 0
    days = _parse_iso_days(iso_days, f"location {location_id}")
    if not days:
        return 0
    return len(recorded_days(location, days))


@shared_task(queue=Queue.MAINTENANCE)
def run_scheduled_demo_account_purge() -> bool:
    """Delete expired demo accounts. A no-op on any instance that is not the demo.

    Unconditionally scheduled (see ``CELERY_BEAT_SCHEDULE``) rather than registered only when
    ``UL_DEMO_MODE`` is on, matching every other entry there - the schedule is fixed at process start,
    and the individual task deciding whether it is due is the existing pattern (see
    ``run_scheduled_database_backup``).

    Returns:
        True when this ran (this is the demo instance), False otherwise.
    """
    from django.core.management import call_command

    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not app_settings.demo_mode:
        return False

    call_command("purge_demo_accounts", execute=True)
    return True


@shared_task(queue=Queue.MAINTENANCE)
@external_background_task("scheduled-redata-public-locations-sync")
def run_scheduled_redata_public_locations_sync() -> bool:
    """Refresh the demo instance's location pool from REData. A no-op everywhere else.

    Unconditionally scheduled, same reasoning as ``run_scheduled_demo_account_purge``.

    Returns:
        True when this ran (this is the demo instance), False otherwise.
    """
    from django.core.management import call_command

    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not app_settings.demo_mode:
        return False

    call_command("import_redata_public_locations")
    return True


@shared_task(queue=Queue.BULK)
def fan_out_wiki_alias_to_pins(alias_id: int) -> int:
    """Mirror one new wiki alias onto every opted-in pin at that location.

    Bulk rather than interactive because the list is as long as the place is
    popular, and nobody is waiting on it - the person who added the name has
    already seen it on the wiki.

    Args:
        alias_id: Primary key of the ``WikiAlias`` that was created.

    Returns:
        How many pins were considered.
    """
    from urbanlens.dashboard.services.aliases.fanout import mirror_wiki_alias_to_pins

    return mirror_wiki_alias_to_pins(alias_id)


@shared_task(queue=Queue.BULK)
def sync_pin_against_smart_lists_task(pin_id: int) -> None:
    """Re-evaluate one pin against every smart list its owner has.

    The hand-off for a profile with more smart lists than a request should walk.

    Args:
        pin_id: Primary key of the pin that was created or edited.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.pin_list_membership import sync_pin_against_smart_lists

    pin = Pin.objects.filter(pk=pin_id).first()
    if pin is not None:
        sync_pin_against_smart_lists(pin, deferred=True)


@shared_task(queue=Queue.INTERACTIVE)
def process_signup(username: str, email: str, password_hash: str, auth_salt: str, invite_token: str | None) -> None:
    """Finish a signup after the response, so a registered address takes no longer to answer than a new one.

    Args:
        username: The validated username.
        email: The address as typed, lowercased.
        password_hash: The already-hashed password.
        auth_salt: The client-side KDF salt, or an empty string.
        invite_token: The invitation token the signup link carried, if any.
    """
    import uuid

    from urbanlens.dashboard.services.auth.signup import complete_signup

    complete_signup(username, email, password_hash, auth_salt, uuid.UUID(invite_token) if invite_token else None)


@shared_task(queue=Queue.INTERACTIVE)
def resend_signup_verification(email: str) -> None:
    """Send a fresh verification link, if ``email`` has an account awaiting one, after the response.

    Args:
        email: The address as typed.
    """
    from urbanlens.dashboard.services.auth.signup import resend_verification

    resend_verification(email)


@shared_task(queue=Queue.INTERACTIVE)
def send_password_reset(email: str) -> None:
    """Send a password-reset link, if ``email`` has an active account, after the response.

    Args:
        email: The address as typed.
    """
    from urbanlens.dashboard.services.auth.signup import send_password_reset as send

    send(email)


@shared_task(queue=Queue.INTERACTIVE)
def deliver_email_claim(claim_id: int) -> None:
    """Send a claimed address its confirmation link, or the in-use notice, after the response.

    Args:
        claim_id: PK of the pending ProfileEmail.
    """
    from urbanlens.dashboard.models.profile.email import ProfileEmail
    from urbanlens.dashboard.services.auth.email_claims import send_confirmation

    claim = ProfileEmail.objects.select_related("profile__user").filter(pk=claim_id, is_verified=False).first()
    if claim is not None:
        send_confirmation(claim)


@shared_task(queue=Queue.BULK)
def announce_trip_change_task(trip_id: int, actor_id: int, change: str) -> int:
    """Tell a trip's members of a change queued by ``change_notifications.announce_trip_change``.

    Args:
        trip_id: PK of the changed trip.
        actor_id: PK of the profile that changed it.
        change: What changed.

    Returns:
        How many members were told; 0 when the trip or the actor is gone.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.services.notifications.change_notifications import notify_trip_change

    trip = Trip.objects.filter(pk=trip_id).first()
    actor = Profile.objects.select_related("user").filter(pk=actor_id).first()
    if trip is None or actor is None:
        return 0
    return notify_trip_change(trip, actor, change)


@shared_task(queue=Queue.BULK)
def announce_wiki_change_task(wiki_id: int, actor_id: int, change: str, fields: list[str]) -> int:
    """Tell a wiki's audience of a change queued by ``change_notifications.announce_wiki_change``.

    Args:
        wiki_id: PK of the changed wiki.
        actor_id: PK of the profile that changed it.
        change: What changed.
        fields: The wiki fields a field edit wrote, else empty.

    Returns:
        How many people were told; 0 when the wiki or the actor is gone.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.notifications.change_notifications import notify_wiki_change

    wiki = Wiki.objects.select_related("location").filter(pk=wiki_id).first()
    actor = Profile.objects.select_related("user").filter(pk=actor_id).first()
    if wiki is None or actor is None:
        return 0
    return notify_wiki_change(wiki, actor, change, fields)
