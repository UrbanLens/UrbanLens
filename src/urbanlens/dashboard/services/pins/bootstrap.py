"""Everything a new root pin's property needs, fetched once in the background rather than when somebody opens it.

``tasks.bootstrap_location`` runs one stage per task run, each with its own time limit, and queues the next:

1. ``boundary`` - ask REData to prewarm the point, then draw the parcel (the boundary panel's own fetch).
2. ``buildings`` - list the parcel's buildings (the "Buildings on this Property" panel's own fetch), which nests them.
3. ``nest`` - the community wiki, then the building places, pins and wikis, for a property whose list was already
   cached by somebody else.
4. ``panels`` - schedule the site panels through the page's own single-flight scheduling.
5. ``dates`` - date the pins from what those fetches cached.

Every fetch takes the panel's flight marker, so a page opened meanwhile polls the bootstrap's fetch instead of making
its own, and a stage that finds a page's fetch in flight waits for it.

Bulk imports never reach here: they create pins through ``Pin.objects.get_nearby_or_create``, and their locations are
filled by the hourly enrichment job, whose per-run cap and per-service budget pace them against the REData key every
environment shares (P144). A client creating root pins one by one through the API is paced by
:data:`BOOTSTRAP_BURST`, and every account together by :data:`SITE_BOOTSTRAP_BURST`; past either, a pin falls back to
the same job.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, NamedTuple

from django.core.cache import cache
from django.db import transaction

from urbanlens.dashboard.services.core.task_limits import SOFT_TIME_LIMIT_ERRORS
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


class BootstrapStage(StrEnum):
    """One step of a property's bootstrap, in the order they run."""

    BOUNDARY = "boundary"
    BUILDINGS = "buildings"
    NEST = "nest"
    PANELS = "panels"
    DATES = "dates"


#: The panels describing the property as a whole: ownership and sales, historic registers, New York's CRIS inventory,
#: news, web photos, reported incidents and the archives searched for documents. Each still passes its own gate.
SITE_PANEL_KEYS: tuple[str, ...] = (
    "property_records",
    "redata_historic_registers",
    "cris_building",
    "gdelt",
    "searxng_images",
    "redata_incidents",
    "loc",
    "internet_archive",
    "smithsonian",
    "digital_commonwealth",
    "chronicling_america",
)

#: Root pins one profile may have bootstrapped at once; a pin past this waits for the hourly enrichment job.
BOOTSTRAP_BURST = 10
#: Seconds for one more of :data:`BOOTSTRAP_BURST` to come back: ten an hour, sustained.
BOOTSTRAP_REFILL_SECONDS = 360
#: Root pins every account together may have bootstrapped at once. At about 25 REData requests each, twenty an hour
#: is half of the hourly budget the key shares with every other environment (P144).
SITE_BOOTSTRAP_BURST = 20
#: Seconds for one more of :data:`SITE_BOOTSTRAP_BURST` to come back: twenty an hour, sustained.
SITE_BOOTSTRAP_REFILL_SECONDS = 180
_SITE_ALLOWANCE_KEY = "ul:pin-bootstrap:site"

#: How many times a stage waits on another caller's fetch of the same data before moving on without it.
MAX_FLIGHT_WAITS = 5
#: Seconds between those waits; five of them outlast a panel fetch task's hard time limit.
FLIGHT_WAIT_SECONDS = 30
#: The longest wait a source may name for a stage to sit it out and ask again, such as REData's while it computes a
#: cold parcel's buildings; past it the stage moves on, and a page view or the enrichment job asks later.
MAX_DEFERRAL_WAIT_SECONDS = 300
#: Added to a source's wait, so the stage asks once the fetch's own suppression has lapsed.
_DEFERRAL_MARGIN_SECONDS = 5
#: How long the bootstrap's own flight marker stands: past ``tasks.bootstrap_location``'s hard time limit, so a killed
#: run's marker expires soon after it, and a page's poll resumes fetching.
_INLINE_FLIGHT_TTL_SECONDS = 300
#: Seconds the dates stage waits for the site panels' fetches; a fetch task's hard limit is under it.
DATES_DELAY_SECONDS = 150

#: How long a location counts as being bootstrapped, for the enrichment job to leave it alone.
IN_FLIGHT_SECONDS = 3600
#: How long a key REData refused to prewarm for is not asked again.
PREWARM_REFUSED_SECONDS = 24 * 3600
#: How long one location is not prewarmed again.
PREWARM_REPEAT_SECONDS = 24 * 3600
_PREWARM_REFUSED_KEY = "ul:redata-prewarm:refused"

#: The queue each stage after the first runs on, when it is not the task's own. A campus can mean hundreds of pins.
_STAGE_QUEUES: dict[BootstrapStage, str] = {BootstrapStage.NEST: Queue.BULK, BootstrapStage.DATES: Queue.BULK}


class FetchOutcome(StrEnum):
    """What asking for one panel's data inline came to."""

    FETCHED = "fetched"
    LANDED = "landed"
    IN_FLIGHT = "in_flight"
    SKIPPED = "skipped"
    #: The source named a wait before it can answer.
    DEFERRED = "deferred"


class Fetched(NamedTuple):
    """What asking for one panel's data inline came to, and the wait a deferring source named."""

    outcome: FetchOutcome
    wait: int | None = None


@dataclass(frozen=True, slots=True)
class NextStep:
    """The stage to queue after this one."""

    stage: BootstrapStage
    attempt: int = 0
    countdown: int | None = None


def _in_flight_key(location_id: int) -> str:
    return f"ul:pin-bootstrap:loc{location_id}"


def bootstrap_eligible(pin: Pin) -> bool:
    """Whether a pin is one a bootstrap may run for.

    Args:
        pin: A newly created pin.

    Returns:
        True for a root pin with coordinates whose owner allows external lookups.
    """
    return pin.parent_pin_id is None and pin.location_id is not None and pin.effective_latitude is not None and pin.effective_longitude is not None and pin.profile.external_apis_enabled


def _take_allowance(pin: Pin) -> bool:
    """Spend one of the profile's bootstraps and one of the site's, or say either has none left this hour."""
    from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage, return_token, take_token

    profile_key, profile_interval_us = f"ul:pin-bootstrap:profile{pin.profile_id}", BOOTSTRAP_REFILL_SECONDS * 1_000_000
    try:
        # The profile's first, so one account looping past its own allowance spends none of everyone else's.
        if not take_token(profile_key, interval_us=profile_interval_us, burst=BOOTSTRAP_BURST, on_outage=Outage.REFUSE):
            return False
        if take_token(_SITE_ALLOWANCE_KEY, interval_us=SITE_BOOTSTRAP_REFILL_SECONDS * 1_000_000, burst=SITE_BOOTSTRAP_BURST, on_outage=Outage.REFUSE):
            return True
    except CounterUnavailableError:
        # It guards a budget other environments share, so an uncountable request is not let through.
        return False
    return_token(profile_key, interval_us=profile_interval_us)
    return False


def request_bootstrap(pin: Pin) -> bool:
    """Queue the bootstrap of a new root pin's property, once the pin is committed.

    Args:
        pin: The pin just created.

    Returns:
        Whether a bootstrap was queued; False leaves the caller's own fallback (and the hourly job) to it.
    """
    if not bootstrap_eligible(pin) or not _take_allowance(pin):
        return False
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import bootstrap_location

    location_id, pin_id = pin.location_id, pin.pk

    def start() -> None:
        # Only once committed: a creation that rolls back must not hold its location for the marker's hour.
        cache.set(_in_flight_key(location_id), pin_id, IN_FLIGHT_SECONDS)
        safely_enqueue_task(bootstrap_location, pin_id)

    transaction.on_commit(start)
    return True


def locations_bootstrapping(location_ids: Iterable[int]) -> set[int]:
    """Which of these locations a bootstrap is filling right now.

    Args:
        location_ids: Candidate location ids.

    Returns:
        The ids with a bootstrap in flight.
    """
    keys = {_in_flight_key(location_id): location_id for location_id in location_ids}
    return {keys[key] for key in cache.get_many(list(keys))} if keys else set()


def enqueue_step(pin_id: int, step: NextStep) -> None:
    """Queue the next stage of a pin's bootstrap.

    Args:
        pin_id: The root pin.
        step: The stage to run.
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import bootstrap_location

    safely_enqueue_task(bootstrap_location, pin_id, stage=step.stage, attempt=step.attempt, countdown=step.countdown, queue=_STAGE_QUEUES.get(step.stage))


def run_stage(pin: Pin, stage: BootstrapStage, attempt: int = 0) -> NextStep | None:
    """Run one stage of a pin's bootstrap.

    A stage that fails is logged and the chain goes on: each later stage stands on its own, and a page visit or the
    enrichment job retries what is missing.

    Args:
        pin: The root pin, with its location and profile.
        stage: The stage to run.
        attempt: How many times this stage has already waited on another caller's fetch.

    Returns:
        The stage to queue next, or None when the chain is done.
    """
    if not bootstrap_eligible(pin):
        cache.delete(_in_flight_key(pin.location_id))
        return None
    try:
        step = _STAGES[stage](pin, attempt)
    except SOFT_TIME_LIMIT_ERRORS:
        logger.warning("bootstrap: stage %s for pin %s ran out of time; moving on", stage, pin.pk)
        step = _after(stage)
    except Exception:
        logger.exception("bootstrap: stage %s failed for pin %s; moving on", stage, pin.pk)
        step = _after(stage)
    if step is None:
        cache.delete(_in_flight_key(pin.location_id))
    return step


def _after(stage: BootstrapStage) -> NextStep | None:
    order = list(BootstrapStage)
    position = order.index(stage)
    return NextStep(order[position + 1]) if position + 1 < len(order) else None


def _waited(stage: BootstrapStage, attempt: int, fetched: Fetched) -> NextStep | None:
    """Wait for another caller's fetch to land, or out a short wait the source named, or move on once waiting has outlasted it."""
    if attempt < MAX_FLIGHT_WAITS:
        if fetched.outcome is FetchOutcome.IN_FLIGHT:
            return NextStep(stage, attempt + 1, FLIGHT_WAIT_SECONDS)
        if fetched.outcome is FetchOutcome.DEFERRED and fetched.wait is not None and fetched.wait <= MAX_DEFERRAL_WAIT_SECONDS:
            return NextStep(stage, attempt + 1, fetched.wait + _DEFERRAL_MARGIN_SECONDS)
    return _after(stage)


def _boundary(pin: Pin, attempt: int) -> NextStep | None:
    if attempt == 0:
        prewarm_location(pin.location)
    return _waited(BootstrapStage.BOUNDARY, attempt, fetch_inline(pin, "boundary"))


def _buildings(pin: Pin, attempt: int) -> NextStep | None:
    return _waited(BootstrapStage.BUILDINGS, attempt, fetch_inline(pin, PARCEL_BUILDINGS_CACHE_SOURCE))


def _nest(pin: Pin, _attempt: int) -> NextStep | None:
    from urbanlens.dashboard.services.pins.auto_nest import auto_nest_pin

    if pin.profile.community_enabled:
        from urbanlens.dashboard.tasks import ensure_wiki_for_location

        # The pin save's own path to the campus wiki, run now so the building wikis have one to nest under; a wiki
        # made any other way skips its Wikipedia seed and its enrichment.
        ensure_wiki_for_location(pin.location_id)
    auto_nest_pin(pin)
    return _after(BootstrapStage.NEST)


def _panels(pin: Pin, _attempt: int) -> NextStep | None:
    scheduled = schedule_site_panels(pin)
    return NextStep(BootstrapStage.DATES, countdown=DATES_DELAY_SECONDS if scheduled else None)


def _dates(pin: Pin, _attempt: int) -> NextStep | None:
    from urbanlens.dashboard.services.pins.build_dates import fill_build_dates

    fill_build_dates(pin)
    return None


_STAGES = {
    BootstrapStage.BOUNDARY: _boundary,
    BootstrapStage.BUILDINGS: _buildings,
    BootstrapStage.NEST: _nest,
    BootstrapStage.PANELS: _panels,
    BootstrapStage.DATES: _dates,
}


def fetch_inline(pin: Pin, source_key: str) -> Fetched:
    """Run one panel's fetch in this task, under the flight marker a page's scheduling uses.

    Args:
        pin: The pin whose panel to fetch.
        source_key: A ``panel_sources()`` key.

    Returns:
        What happened: fetched, already landed, in flight elsewhere, deferred by the source for a wait it named, or
        not applicable.
    """
    from urbanlens.dashboard.services.core.locks import acquire_lock
    from urbanlens.dashboard.services.pins.external_data import fetch_blocked, gate_allows, get_panel_source, run_panel_fetch

    source = get_panel_source(source_key)
    if source is None or not gate_allows(source, pin):
        return Fetched(FetchOutcome.SKIPPED)
    if source.has_landed(pin):
        return Fetched(FetchOutcome.LANDED)
    if fetch_blocked(source, pin):
        return Fetched(FetchOutcome.SKIPPED)
    token = acquire_lock(source.flight_key(pin), _INLINE_FLIGHT_TTL_SECONDS)
    if token is None:
        return Fetched(FetchOutcome.IN_FLIGHT)
    wait = run_panel_fetch(source_key, pin, token)
    if wait is not None and not source.has_landed(pin):
        return Fetched(FetchOutcome.DEFERRED, wait)
    return Fetched(FetchOutcome.FETCHED)


def schedule_site_panels(pin: Pin) -> list[str]:
    """Schedule every site panel that applies to the pin and has nothing stored, as a page's first view would.

    Args:
        pin: A root pin.

    Returns:
        The keys of the panels now being fetched, by this call or one already in flight.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.pins.external_data import gate_allows, get_panel_source, panel_visible_to, schedule_panel_fetch

    # Every one of them answers from REData; without it there is nothing to schedule.
    if not redata_configured():
        return []
    scheduled: list[str] = []
    for key in SITE_PANEL_KEYS:
        source = get_panel_source(key)
        if source is None or not panel_visible_to(pin.profile.user, source) or not gate_allows(source, pin) or source.has_landed(pin):
            continue
        if schedule_panel_fetch(key, pin):
            scheduled.append(key)
    return scheduled


def prewarm_location(location: Location) -> bool:
    """Ask REData to start fetching what it knows about a new point, without waiting for any of it.

    The key needs the ``locations:prewarm`` scope; one without it is remembered and not asked again for
    :data:`PREWARM_REFUSED_SECONDS`, and the bootstrap goes on as if nothing had been asked.

    Args:
        location: The newly pinned location.

    Returns:
        Whether REData accepted the request.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.apis.locations.redata_prewarm_gateway import PrewarmNotPermittedError, RedataPrewarmGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    if location.latitude is None or location.longitude is None or not redata_configured() or cache.get(_PREWARM_REFUSED_KEY):
        return False
    if not cache.add(f"ul:redata-prewarm:loc{location.pk}", 1, PREWARM_REPEAT_SECONDS):
        return False
    try:
        RedataPrewarmGateway().prewarm(float(location.latitude), float(location.longitude))
    except PrewarmNotPermittedError:
        logger.info("REData does not let this key prewarm; not asking again for %ss", PREWARM_REFUSED_SECONDS)
        cache.set(_PREWARM_REFUSED_KEY, 1, PREWARM_REFUSED_SECONDS)
        return False
    except GatewayRequestError as exc:
        # Unreachable, throttled or refused by our own rate limiter: the bootstrap's fetches ask REData directly anyway.
        logger.info("REData prewarm for location %s not made: %s", location.pk, type(exc).__name__)
        return False
    return True
