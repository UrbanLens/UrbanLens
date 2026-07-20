"""Scheduled background enrichment of Locations (and thereby pins and wikis).

Most external data is fetched lazily - a pin detail page schedules a panel
fetch, a new wiki triggers :func:`~urbanlens.dashboard.tasks.enrich_wiki_location`,
and so on. A small set of high-value data (official names, aliases, street
addresses, and property/building boundaries) is worth having for *every*
location up front, so an hourly Celery task (``tasks.run_scheduled_enrichment``)
drips those fetches into whatever API budget is left over after real traffic.

The design has three moving parts:

1. :class:`EnrichmentSource` - one kind of enrichable data. Core sources
   (street address, boundaries) live in this module; integration-specific
   sources are contributed by plugins via
   :meth:`~urbanlens.dashboard.plugins.base.UrbanLensPlugin.get_enrichment_sources`,
   which keeps the system extensible. Each source declares which rate-limited
   service keys it consumes and knows which Locations still lack its data -
   completion is tracked *per source* (usually via the presence of that
   source's ``LocationCache`` row, regardless of freshness), so one provider
   having run never masks another that hasn't.
2. :func:`compute_service_budget` - how many API calls a service can spare
   right now. Every window keeps ``enrichment_buffer_percent`` (default 10%)
   of the configured limit in reserve for traffic spikes, and multi-day
   windows are spread evenly so the whole 30-day budget can't be burned in a
   day (e.g. a 300-calls/30-days limit with 6 calls already made today
   yields ``270 // 30 - 6 = 3``).
3. :func:`run_enrichment_cycle` - one pass: per source, compute the budget,
   pick the highest-impact candidate Locations, enrich them sequentially with
   a per-item stagger derived from the service's per-minute limit (so even
   very generous limits are never hammered in a burst), then re-resolve
   official names/aliases once per touched location.

Everything is admin-tunable via ``SiteSettings`` (enable toggle, UTC run
window, buffer percent, per-service per-run cap) on the site-admin page.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
import logging
import math
import time
from typing import TYPE_CHECKING, Any, ClassVar

from celery.exceptions import SoftTimeLimitExceeded
from django.db.models import Q

from urbanlens.dashboard.services.rate_limiter import RequestCancelledError, get_limit_config, service_is_enabled

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.services.geo_boundary import GeoBoundary

logger = logging.getLogger(__name__)

#: Django-cache key holding the last cycle's summary (shown on the site-admin page).
LAST_RUN_CACHE_KEY = "ul_enrichment_last_run"

#: Django-cache key used as the single-flight lock for the hourly task.
RUN_LOCK_CACHE_KEY = "ul_enrichment_running"

#: Bounds on the per-item pause between API calls within one source's batch.
MIN_STAGGER_SECONDS = 1.0
MAX_STAGGER_SECONDS = 60.0

#: Default pause when none of a source's services declare a per-minute limit.
DEFAULT_STAGGER_SECONDS = 2.0

#: How many extra candidates beyond the budget are shortlisted so the
#: nearby-pin-density signal can reorder them before the final cut.
_DENSITY_SHORTLIST_FACTOR = 3

#: Radius and score cap for the nearby-pin-density prioritization signal.
_DENSITY_RADIUS_KM = 2.0
_DENSITY_SCORE_CAP = 20


class EnrichmentSource(ABC):
    """One kind of background-enrichable data for a Location.

    Subclasses declare which rate-limited services they consume and implement
    the "which locations still need this" filter plus the actual fetch. Core
    sources are registered in :func:`enrichment_sources`; integrations
    contribute theirs via ``UrbanLensPlugin.get_enrichment_sources`` so new
    enrichment kinds can be added without touching this module.

    Attributes:
        key: Unique slug identifying this source in run summaries and logs.
        verbose_name: Human-readable name for the admin UI; defaults to key.
        service_keys: Rate-limiter service keys consumed per enriched item.
            The per-run budget is the *minimum* budget across these services.
        calls_per_item: Estimated API calls one :meth:`enrich` makes; budgets
            are divided by this so a two-call source gets half the items.
        geo_boundary: When set, candidate locations are restricted to this
            geographic region (see ``services.geo_boundary``); None means
            unrestricted.
        refreshes_names: When True, official names/aliases are re-resolved for
            every location this source successfully enriches in a cycle.
    """

    key: ClassVar[str] = ""
    verbose_name: ClassVar[str] = ""
    service_keys: ClassVar[tuple[str, ...]] = ()
    calls_per_item: ClassVar[int] = 1
    geo_boundary: ClassVar[GeoBoundary | None] = None
    refreshes_names: ClassVar[bool] = False

    def gate(self) -> bool:
        """Whether this source is usable at all on this install (e.g. API key set).

        Returns:
            True when the source can run; False skips it for the whole cycle.
        """
        return True

    @abstractmethod
    def missing_filter(self) -> Q:
        """Filter selecting Locations that still lack this source's data.

        This is the per-source completion tracker: it must consider only this
        source's own marker (its ``LocationCache`` row, a stamped column, ...)
        so sources are tracked independently of one another. "Attempted but
        found nothing" must count as complete - otherwise the same hopeless
        location would be retried every cycle, burning budget forever.

        Returns:
            A ``Q`` usable in ``Location.objects.filter``.
        """

    @abstractmethod
    def enrich(self, location: Location) -> bool:
        """Fetch and persist this source's data for one location.

        Implementations must persist a completion marker even when the
        upstream API finds nothing (see :meth:`missing_filter`).

        Args:
            location: The location to enrich.

        Returns:
            True when data (or an empty "nothing found" marker) was stored.
        """


class LocationCacheEnrichmentSource(EnrichmentSource):
    """Base for sources whose data and completion marker is a ``LocationCache`` row.

    The *existence* of the row - fresh or stale - marks the source as having
    run for a location, so background enrichment only ever backfills
    never-fetched locations; refreshing stale rows stays the job of the lazy
    panel-fetch machinery that already knows a user is looking.
    """

    cache_source: ClassVar[str] = ""
    refreshes_names: ClassVar[bool] = True

    def missing_filter(self) -> Q:
        """Locations with no ``LocationCache`` row (of any age) for this source."""
        from django.db.models import Exists, OuterRef

        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        return ~Q(Exists(LocationCache.objects.filter(location=OuterRef("pk"), source=self.cache_source)))

    def enrich(self, location: Location) -> bool:
        """Fetch upstream data and upsert the cache row (empty dict = "nothing found")."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        data, query_key = self.fetch(location)
        LocationCache.set(location, self.cache_source, data or {}, query_key=query_key)
        return True

    @abstractmethod
    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Fetch this source's payload for one location.

        Args:
            location: The location to fetch data for.

        Returns:
            Tuple of (payload dict or None when nothing was found, query key
            recorded on the cache row).
        """


class AddressEnrichmentSource(EnrichmentSource):
    """Backfills street-address components via the Google Geocoding API.

    Uses the same :func:`~urbanlens.dashboard.services.locations.addresses.ensure_location_address`
    helper the pin-edit page uses lazily. Because a failed geocode leaves the
    address columns empty, completion is tracked with a dedicated
    ``address_backfill`` LocationCache marker rather than the columns
    themselves - one attempt per location, ever.
    """

    key: ClassVar[str] = "address"
    verbose_name: ClassVar[str] = "Street address (Google Geocoding)"
    service_keys: ClassVar[tuple[str, ...]] = ("google_geocoding",)

    #: LocationCache source recording that a backfill attempt happened.
    marker_source: ClassVar[str] = "address_backfill"

    def gate(self) -> bool:
        """Requires the unrestricted Google API key."""
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        return bool(app_settings.google_unrestricted_api_key)

    def missing_filter(self) -> Q:
        """Locations with no street route and no prior backfill attempt."""
        from django.db.models import Exists, OuterRef

        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        attempted = LocationCache.objects.filter(location=OuterRef("pk"), source=self.marker_source)
        return (Q(route__isnull=True) | Q(route="")) & ~Q(Exists(attempted))

    def enrich(self, location: Location) -> bool:
        """Reverse-geocode the address and record the attempt marker."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.locations.addresses import ensure_location_address

        resolved = ensure_location_address(location)
        LocationCache.set(
            location,
            self.marker_source,
            {"resolved": resolved},
            query_key=f"{location.latitude},{location.longitude}",
        )
        return True


class BoundaryEnrichmentSource(EnrichmentSource):
    """Generates default property/building boundaries via the boundary provider chain.

    ``generate_location_boundaries`` stamps ``generated_at`` even when nothing
    is found, so each location is attempted exactly once. Overpass is the
    chain's first (and rate-tightest) provider, so it anchors the budget; the
    remaining footprint providers are guarded by their own rate-limit rows at
    call time.
    """

    key: ClassVar[str] = "boundary"
    verbose_name: ClassVar[str] = "Property/building boundaries"
    service_keys: ClassVar[tuple[str, ...]] = ("overpass",)
    calls_per_item: ClassVar[int] = 2

    def missing_filter(self) -> Q:
        """Locations whose location-default property boundary was never generated."""
        from django.db.models import Exists, OuterRef

        from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

        generated = Boundary.objects.filter(
            location=OuterRef("pk"),
            boundary_type=BoundaryType.PROPERTY,
            pin=None,
            wiki=None,
            profile=None,
            generated_at__isnull=False,
        )
        return ~Q(Exists(generated))

    def enrich(self, location: Location) -> bool:
        """Run the boundary provider chain and persist the generated geometry."""
        from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, generate_location_boundaries

        if boundary_generation_ran(location):
            return False
        generate_location_boundaries(location)
        return True


def enrichment_sources() -> list[EnrichmentSource]:
    """Every active enrichment source: core ones plus plugin contributions.

    Returns:
        Sources in registration order, deduplicated by ``key`` (first wins).
    """
    from urbanlens.dashboard.plugins.registry import plugin_registry

    sources: list[EnrichmentSource] = [
        AddressEnrichmentSource(),
        BoundaryEnrichmentSource(),
    ]
    sources.extend(plugin_registry.enrichment_sources())

    seen: set[str] = set()
    unique: list[EnrichmentSource] = []
    for source in sources:
        if not source.key or source.key in seen:
            logger.warning("Ignoring enrichment source %r: missing or duplicate key", type(source).__qualname__)
            continue
        seen.add(source.key)
        unique.append(source)
    return unique


def compute_service_budget(service: str, site_settings: SiteSettings | None = None) -> int | None:
    """How many API calls background enrichment may spend on a service right now.

    Keeps ``enrichment_buffer_percent`` of every configured limit in reserve
    for organic traffic spikes, and spreads multi-day windows evenly so one
    cycle can't burn a month's budget: with a 300-calls/30-days limit and a
    10% buffer, the drip allowance is ``270 // 30 = 9`` calls per rolling day,
    so 6 calls already made in the last 24 hours leaves a budget of 3.

    Windows are measured against ``ApiCallLog`` with rolling lookbacks
    (last 24 hours / last 30 days), matching how ``check_rate_limit`` counts
    its 30-day window and staying strictly more conservative than its
    calendar-day daily count. Geo-filtered rows never hit the network, so
    they are excluded just as the rate limiter excludes them.

    Args:
        service: The rate-limiter service key.
        site_settings: Current settings; fetched when omitted.

    Returns:
        Remaining call budget (never negative), ``0`` when the service is
        disabled or exhausted, or ``None`` when the service configures
        neither a daily nor a 30-day limit (i.e. unbounded - callers should
        apply their own per-run cap).
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    if site_settings is None:
        site_settings = SiteSettings.get_current()

    try:
        config = get_limit_config(service)
    except Exception:
        # TODO: Catch specific exceptions
        logger.exception("compute_service_budget: failed to read rate limit config for %s", service)
        return 0
    if not config.enabled:
        return 0

    buffer_fraction = min(max(site_settings.enrichment_buffer_percent, 0), 90) / 100.0

    def used_within(delta: timedelta) -> int:
        return ApiCallLog.objects.for_service(service).since(delta).exclude(was_geo_filtered=True).count()

    budgets: list[int] = []
    used_day: int | None = None

    if config.calls_per_day is not None:
        used_day = used_within(timedelta(hours=24))
        effective_day = math.floor(config.calls_per_day * (1 - buffer_fraction))
        budgets.append(effective_day - used_day)

    if config.calls_per_30_days is not None:
        if used_day is None:
            used_day = used_within(timedelta(hours=24))
        effective_30 = math.floor(config.calls_per_30_days * (1 - buffer_fraction))
        used_30 = used_within(timedelta(days=30))
        daily_allowance = effective_30 // 30
        budgets.append(min(effective_30 - used_30, daily_allowance - used_day))

    if not budgets:
        return None
    return max(0, min(budgets))


def stagger_seconds(source: EnrichmentSource) -> float:
    """Pause between one source's consecutive enrichments, from its per-minute limits.

    Even a service with an enormous daily limit shouldn't see a burst of
    back-to-back requests from the background job, so the pause is derived
    from the tightest per-minute limit among the source's services and
    clamped to [MIN_STAGGER_SECONDS, MAX_STAGGER_SECONDS].

    Args:
        source: The enrichment source about to run a batch.

    Returns:
        Seconds to sleep between items.
    """
    per_minute_limits: list[int] = []
    for service in source.service_keys:
        try:
            limit = get_limit_config(service).calls_per_minute
        except Exception:
            # TODO: Catch specific exceptions
            logger.exception("stagger_seconds: failed to read rate limit config for %s", service)
            continue
        if limit:
            per_minute_limits.append(limit)

    if not per_minute_limits:
        return DEFAULT_STAGGER_SECONDS
    pause = (60.0 / min(per_minute_limits)) * max(source.calls_per_item, 1)
    return min(max(pause, MIN_STAGGER_SECONDS), MAX_STAGGER_SECONDS)


def enrichment_window_open(site_settings: SiteSettings, *, now: datetime | None = None) -> bool:
    """Whether the admin-scheduled daily run window currently allows enrichment.

    The window is ``[start, end)`` in UTC hours and may wrap midnight
    (e.g. 22 -> 4). Equal start and end means "any hour".

    Args:
        site_settings: Current settings holding the configured hours.
        now: Injected current time for tests; defaults to ``timezone.now()``.

    Returns:
        True when a cycle may run now.
    """
    from django.utils import timezone

    start = site_settings.enrichment_start_hour
    end = site_settings.enrichment_end_hour
    if start == end:
        return True
    hour = (now or timezone.now()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def prioritized_location_candidates(missing: Q, *, limit: int, geo_boundary: GeoBoundary | None = None) -> list[Location]:
    """Pick the Locations most worth enriching next, highest impact first.

    Impact scoring favors places more users will actually see:

    * distinct users with a pin at the location (x3),
    * pin-list memberships across those pins (x2),
    * an existing community wiki (+2),
    * raw pin count (x1),
    * and, as a refinement among the shortlisted leaders, the number of other
      pinned locations within ~2 km (capped) - a proxy for high-traffic areas.

    Only locations somebody actually references (a pin or a wiki) are
    considered; orphaned Location rows can wait for lazy loading.

    Args:
        missing: The source's :meth:`EnrichmentSource.missing_filter`.
        limit: Maximum candidates to return (the per-run item budget).
        geo_boundary: Restrict to locations within this region (a real
            PostGIS spatial filter against ``Location.point``), or None for
            no restriction.

    Returns:
        Up to ``limit`` locations, best candidates first.
    """
    from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Value, When

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    if limit <= 0:
        return []

    queryset = (
        Location.objects.filter(missing)
        .annotate(
            has_wiki=Exists(Wiki.objects.filter(location=OuterRef("pk"))),
            has_pin=Exists(Pin.objects.filter(location=OuterRef("pk"))),
        )
        .filter(Q(has_wiki=True) | Q(has_pin=True))
        .annotate(
            profile_count=Count("pins__profile", distinct=True),
            pin_count=Count("pins", distinct=True),
            list_count=Count("pins__list_memberships", distinct=True),
        )
        .annotate(
            priority_score=(F("profile_count") * Value(3) + F("list_count") * Value(2) + Case(When(has_wiki=True, then=Value(2)), default=Value(0), output_field=IntegerField()) + F("pin_count")),
        )
        .order_by("-priority_score", "-updated")
    )
    if geo_boundary is not None and geo_boundary.geometry is not None:
        queryset = queryset.filter(point__within=geo_boundary.geometry)

    shortlist = list(queryset[: limit * _DENSITY_SHORTLIST_FACTOR])
    if len(shortlist) > limit:
        scored = [(candidate.priority_score + _nearby_density_score(candidate), candidate) for candidate in shortlist]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [candidate for _score, candidate in scored[:limit]]
    return shortlist


def _nearby_density_score(location: Location) -> int:
    """Capped count of other Locations within ~2 km, as a high-traffic-area proxy.

    Args:
        location: The candidate location.

    Returns:
        The neighbor count, capped at ``_DENSITY_SCORE_CAP``.
    """
    from django.contrib.gis.measure import D

    from urbanlens.dashboard.models.location.model import Location

    if location.point is None:
        return 0
    count = Location.objects.filter(point__distance_lte=(location.point, D(km=_DENSITY_RADIUS_KM))).exclude(pk=location.pk).count()
    return min(count, _DENSITY_SCORE_CAP)


def run_enrichment_cycle(*, force: bool = False, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Run one background-enrichment pass across every source.

    For each source: verify availability, compute the per-run item budget
    (minimum service budget divided by calls-per-item, capped by the admin's
    per-service-per-run limit), pick the highest-impact candidate locations,
    and enrich them sequentially with a stagger pause between items. Official
    names and aliases are re-resolved once per touched location at the end,
    reading only the freshly cached data (no extra API calls).

    Args:
        force: Ignore the enabled toggle and run window (admin "run now").
        sleep: Injected pause function for tests; defaults to ``time.sleep``.

    Returns:
        A summary dict (also cached at ``LAST_RUN_CACHE_KEY``) with per-source
        enriched/failed counts and skip reasons.
    """
    from django.core.cache import cache
    from django.utils import timezone

    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    site_settings = SiteSettings.get_current()
    summary: dict[str, Any] = {"started": timezone.now().isoformat(), "sources": {}}

    if not force:
        if not site_settings.enrichment_enabled:
            summary["skipped"] = "disabled"
            return summary
        if not enrichment_window_open(site_settings):
            summary["skipped"] = "outside_window"
            return summary

    per_run_cap = max(1, site_settings.enrichment_max_per_service_per_run)
    name_refresh_ids: set[int] = set()

    for source in enrichment_sources():
        entry: dict[str, Any] = {"enriched": 0, "failed": 0, "budget": 0}
        summary["sources"][source.key] = entry
        try:
            self_reported = self_reported_skip(source)
            if self_reported:
                entry["skipped"] = self_reported
                continue

            budgets = [compute_service_budget(service, site_settings) for service in source.service_keys]
            bounded = [budget for budget in budgets if budget is not None]
            call_budget = min(bounded) if bounded else None
            items = per_run_cap if call_budget is None else min(per_run_cap, call_budget // max(source.calls_per_item, 1))
            entry["budget"] = max(0, items)
            if items <= 0:
                entry["skipped"] = "no_budget"
                continue

            candidates = prioritized_location_candidates(source.missing_filter(), limit=items, geo_boundary=source.geo_boundary)
            if not candidates:
                entry["skipped"] = "nothing_missing"
                continue

            pause = stagger_seconds(source)
            for index, location in enumerate(candidates):
                if index:
                    sleep(pause)
                try:
                    changed = source.enrich(location)
                except SoftTimeLimitExceeded:
                    raise
                except RequestCancelledError as exc:
                    # The service hit its live rate limit or was disabled
                    # mid-run - stop this source, let the others continue.
                    logger.info("Enrichment source %s stopped early: %s", source.key, exc)
                    entry["skipped"] = "rate_limited"
                    break
                except Exception:
                    # TODO: Catch specific exceptions
                    logger.exception("Enrichment source %s failed for location %s", source.key, location.pk)
                    entry["failed"] += 1
                    continue
                if changed:
                    entry["enriched"] += 1
                    if source.refreshes_names:
                        name_refresh_ids.add(location.pk)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            # A broken source must never take down the whole cycle.
            # TODO: Catch specific exceptions
            logger.exception("Enrichment source %s crashed", source.key)
            entry["skipped"] = "error"

    if name_refresh_ids:
        summary["names_refreshed"] = refresh_official_names(name_refresh_ids)

    summary["finished"] = timezone.now().isoformat()
    cache.set(LAST_RUN_CACHE_KEY, summary, None)

    totals = summary["sources"].values()
    logger.info(
        "Enrichment cycle complete: %d enriched, %d failed across %d source(s)",
        sum(entry["enriched"] for entry in totals),
        sum(entry["failed"] for entry in totals),
        len(summary["sources"]),
    )
    return summary


def self_reported_skip(source: EnrichmentSource) -> str | None:
    """Why a source can't run at all this cycle, or None when it can.

    Args:
        source: The enrichment source to check.

    Returns:
        ``"unavailable"`` when the source's own gate fails (e.g. missing API
        key), ``"service_disabled"`` when any of its services is switched off
        on the API-limits page, else None.
    """
    if not source.gate():
        return "unavailable"
    if not all(service_is_enabled(service) for service in source.service_keys):
        return "service_disabled"
    return None


def refresh_official_names(location_ids: Iterable[int]) -> int:
    """Re-resolve official names and aliases for freshly enriched locations.

    Reads only cached candidates (the rows enrichment just wrote), so this
    makes no API calls of its own.

    Args:
        location_ids: PKs of locations whose caches changed this cycle.

    Returns:
        Number of locations whose name, wiki name, or alias list changed.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

    refreshed = 0
    for location in Location.objects.filter(pk__in=set(location_ids)):
        try:
            if update_location_name_from_external_sources(location):
                refreshed += 1
        except Exception:
            # TODO: Catch specific exceptions
            logger.exception("Name refresh failed for location %s after enrichment", location.pk)
    return refreshed


def last_run_summary() -> dict[str, Any] | None:
    """The most recent cycle's summary, for the site-admin page.

    Returns:
        The summary dict cached by :func:`run_enrichment_cycle`, or None
        when no cycle has completed since the cache was last cleared.
    """
    from django.core.cache import cache

    summary = cache.get(LAST_RUN_CACHE_KEY)
    return summary if isinstance(summary, dict) else None
