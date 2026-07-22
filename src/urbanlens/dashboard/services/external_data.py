"""Background-fetch orchestration for the pin detail page's external-data panels.

Every external-data panel (Wikipedia, media archives, satellite imagery,
default boundaries, ...) used to fetch its upstream data inside the HTTP request
that rendered it, bounded only by a wall-clock deadline. That kept slow
providers from hanging a single request, but the work still happened on the
web worker: a cold pin page fired ~10 upstream fetches through the request
path at once, and CPU-bound steps (gunzipping building-footprint shards,
shapely geometry work) blocked the gevent event loop outright, which no
timeout can prevent.

This module moves all of that off the request path:

* Each panel is described by a :class:`PanelSource` -- it knows how to check
  whether its data has already landed in its backing store (``is_ready``) and
  how to fetch-and-persist that data (``fetch``, run inside a Celery worker).
* Controllers call :func:`schedule_panel_fetch` on a cache miss and return a
  small self-polling placeholder instead of blocking; the HTMX fragment polls
  until the task lands the data (or gives up after
  :data:`MAX_POLL_ATTEMPTS`).
* Scheduling is single-flight per (source, target): an atomic ``cache.add``
  ensures concurrent page loads share one task instead of stampeding the
  upstream API.
* A failed or disabled source sets a short-lived "skip" marker so its panel
  degrades to an immediate 204 (quietly absent) instead of re-polling every
  page load; the source resumes automatically when the marker expires.

Adding a new panel means writing one ``PanelSource`` subclass inside a
plugin (see :mod:`urbanlens.dashboard.plugins`), returning it from the
plugin's ``get_panel_sources``, and pointing a template fragment at a
controller that follows the ready-render-or-schedule pattern -- the task
plumbing, deduplication, and failure handling are shared. The satellite and
street-view carousels similarly assemble their provider chains from plugins'
``get_satellite_providers``/``get_street_view_providers`` contributions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, ClassVar

from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.utils import timezone

from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError, RequestCancelledError, ServiceDisabledError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaProvider
    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide
    from urbanlens.dashboard.services.geo_boundary import GeoBoundary

logger = logging.getLogger(__name__)

#: Seconds between HTMX/JS poll requests while a fetch task is in flight.
POLL_INTERVAL_SECONDS = 2
#: Polls before a panel gives up and disappears for this page view. The next
#: full page load starts a fresh poll cycle, so this only bounds how long one
#: page keeps asking, not how long the data takes to eventually land.
MAX_POLL_ATTEMPTS = 30
#: TTL for the single-flight marker. Must comfortably exceed the Celery task's
#: hard time limit so a killed task's marker expires right after the task does,
#: and a crashed worker can't wedge a panel for longer than this.
FLIGHT_TTL_SECONDS = 150
#: How long a source stays suppressed after its fetch failed unexpectedly.
FAILURE_SKIP_TTL_SECONDS = 300
#: How long a source stays suppressed after reporting itself rate-limited or
#: administratively disabled. Longer than the failure TTL: these are explicit
#: signals, not transient flakes.
DISABLED_SKIP_TTL_SECONDS = 1800
#: TTL for the satellite/street "caches are warm" marker. Deliberately shorter
#: than the 24h per-provider slide caches it summarises, so the marker always
#: expires (and re-warms via a task) before the underlying entries do.
SLIDES_READY_TTL_SECONDS = 12 * 3600


@dataclass(frozen=True, slots=True)
class ProviderFetchResult:
    """Outcome of one imagery provider inside a slide collector run.

    Attributes:
        service: The provider's service key (or class name when keyless).
        from_cache: Whether the provider's slides came from its Django cache.
        count: Number of slides the provider contributed.
        ok: False when the provider raised instead of returning slides.
    """

    service: str
    from_cache: bool
    count: int
    ok: bool = True


class PanelSource(ABC):
    """One external-data panel: readiness check plus Celery-side fetch.

    Subclasses define where the panel's data lives and how to fill it. The
    scheduling, single-flight, and failure-suppression machinery in this
    module is shared and driven purely through this interface.

    Attributes:
        key: Registry key; also the Celery task argument and log label.
        section_id: DOM id of the panel's section element (HTMX panels only).
        icon: Material symbol name for the pending placeholder's header.
        title: Heading text for the pending placeholder's header.
        outer_class: CSS classes for the pending placeholder's outer element.
        outer_is_card: True when the section element is itself the card (the
            satellite/street layout) rather than wrapping an inner card div.
        queue: Celery queue this source's fetch is dispatched to. Defaults to
            the dedicated ``panel_fetch`` queue (a high-concurrency thread
            pool - see docker-compose.yml's celery-worker-panels service),
            appropriate for the common case of "one or two small HTTP calls."
            Override to ``"celery"`` (the default queue, prefork pool) for a
            source whose fetch does real CPU-bound work (e.g. Overture's
            GeoParquet/Shapely geometry parsing) - many of those running at
            once on a thread pool would cause GIL contention that slows down
            every other panel sharing it, defeating the point of splitting
            the queue in the first place.
    """

    key: ClassVar[str]
    section_id: ClassVar[str] = ""
    icon: ClassVar[str] = "public"
    title: ClassVar[str] = ""
    outer_class: ClassVar[str] = ""
    outer_is_card: ClassVar[bool] = False
    queue: ClassVar[str] = "panel_fetch"

    def scope(self, pin: Pin) -> str:
        """Cache-key scope identifying which rows/entries this pin's fetch fills.

        Location-scoped by default, because most panels cache per shared
        Location (two users pinning the same place share one fetch).

        Args:
            pin: The pin whose panel is being fetched.

        Returns:
            A short string unique to the fetch target.
        """
        return f"loc{pin.location_id}"

    def flight_key(self, pin: Pin) -> str:
        """Single-flight cache key for this source and pin's fetch target."""
        return f"ulfetch:flight:{self.key}:{self.scope(pin)}"

    def skip_key(self, pin: Pin) -> str:
        """Suppression cache key set after a failed/disabled fetch."""
        return f"ulfetch:skip:{self.key}:{self.scope(pin)}"

    def gate(self, pin: Pin) -> bool:
        """Whether this source has enough information to fetch for ``pin``.

        Checked before scheduling a fetch so a source with nothing to work
        with (e.g. no coordinates, no address, no name) degrades to a quiet
        204 instead of polling forever. The default always allows the fetch;
        override when a source needs a precondition beyond "has a Location".

        Args:
            pin: The pin whose panel is being rendered.

        Returns:
            True when a fetch is worth scheduling.
        """
        return True

    @abstractmethod
    def is_ready(self, pin: Pin) -> bool:
        """Whether the panel's data has already been fetched and persisted.

        Args:
            pin: The pin whose panel is being rendered.

        Returns:
            True when the controller can render directly from the store.
        """

    @abstractmethod
    def fetch(self, pin: Pin) -> None:
        """Fetch from the upstream provider(s) and persist to the panel's store.

        Runs inside a Celery worker, never on the request path. Implementations
        persist their own results (LocationCache row, Boundary column, Django
        cache entries) including an explicit empty result when the provider
        genuinely found nothing -- an absent store entry means "not fetched
        yet", and an empty one means "fetched, nothing there".

        Args:
            pin: The pin whose panel data should be fetched.
        """


class LocationCachePanelSource(PanelSource, ABC):
    """Base for panels whose store is a ``LocationCache`` row.

    Attributes:
        cache_source: The LocationCache ``source`` field value this panel
            reads and writes.
    """

    cache_source: ClassVar[str]

    def is_ready(self, pin: Pin) -> bool:
        """True when a fresh LocationCache row exists for this source."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        return LocationCache.get_fresh(pin.location, self.cache_source) is not None


class InfoPanelSource(LocationCachePanelSource, ABC):
    """Base for panels that render through the generic ``_simple_info_panel.html`` template.

    A subclass owns only ``fetch`` (writing to its ``LocationCache`` row,
    inherited from ``LocationCachePanelSource``) and ``render_context``
    (turning that row's cached data into the template's context shape). The
    URL, controller dispatch, readiness/pending polling, and debug-overlay
    wiring are all fully generic (see ``PinController.panel_info``), so a new
    panel of this shape needs only a new ``InfoPanelSource`` subclass in a
    plugin - no new route, controller method, or template block.

    Panels with genuinely bespoke markup (their own JS, a listings grid, a
    map, ...) don't fit this shape and should keep a dedicated controller
    method, route, and template instead of forcing themselves in here.
    """

    @abstractmethod
    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build ``_simple_info_panel.html``'s context from cached data.

        ``section_id``/``icon``/``title`` are filled in by the caller from
        this source's own class attributes - don't include them here.

        Args:
            pin: The pin whose panel is being rendered.
            data: The ``LocationCache`` row's ``data`` dict (``{}`` when the
                fetch found nothing).

        Returns:
            A context dict (may include ``heading_name``, ``chips``,
            ``meta``, ``header_link``, ``footer_link``), or None when there's
            nothing worth showing (renders a 204).
        """

    def debug_count(self, data: dict) -> int:
        """Item count reported in the debug overlay.

        Defaults to 1 (one record found); override for panels whose cached
        data represents a list of distinct results.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.
        """
        return 1


class CoordinateGatedInfoPanelSource(InfoPanelSource, ABC):
    """An ``InfoPanelSource`` that only makes sense when the pin has coordinates.

    Attributes:
        geo_boundary: Restricts this panel to a geographic region (see
            ``services.geo_boundary``); None means unrestricted.
    """

    geo_boundary: ClassVar[GeoBoundary | None] = None

    def gate(self, pin: Pin) -> bool:
        """Skip scheduling a fetch for a pin with no usable coordinates, or outside ``geo_boundary``."""
        lat, lng = pin.effective_latitude, pin.effective_longitude
        if not (lat and lng):
            return False
        return self.geo_boundary is None or self.geo_boundary.contains(lat, lng)


class GalleryMediaSource(LocationCachePanelSource, ABC):
    """Base for anything that can appear as a source tab in the Media gallery.

    The pin detail page's Media gallery combines results from several
    unrelated providers (archive/media search engines, business directories,
    imagery APIs, ...) behind one uniform per-source loader/tab. Each
    provider needs only its own ``fetch`` (writing to its ``LocationCache``
    row, inherited scheduling/readiness/failure handling from
    ``LocationCachePanelSource``) and ``media_items`` (turning that row's
    ``data`` back into displayable items) - the gallery controller and
    template are otherwise oblivious to which provider it's rendering.
    """

    @abstractmethod
    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn this source's cached ``LocationCache.data`` into gallery items.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source
                (``{}`` when the fetch found nothing).

        Returns:
            The items to render as ``.media-item`` tiles; may be empty.
        """


class MediaPanelSource(GalleryMediaSource):
    """One provider of the combined Media gallery (Smithsonian, Wikimedia, LOC).

    Instantiated once per provider; ``make_gateway`` builds the concrete
    :class:`MediaProvider`, whose ``get_media`` owns the LocationCache write.
    """

    def __init__(self, key: str, cache_source: str, gateway_factory) -> None:
        """Bind this source to one media provider.

        Args:
            key: Registry key, matching the URL's ``source`` segment.
            cache_source: The provider gateway's ``service_key`` (its
                LocationCache source).
            gateway_factory: Zero-argument callable building the gateway.
        """
        # Per-instance rather than ClassVar: three providers share this class.
        self.key = key
        self.cache_source = cache_source
        self._gateway_factory = gateway_factory

    def make_gateway(self) -> MediaProvider:
        """Build this provider's gateway instance."""
        return self._gateway_factory()

    @staticmethod
    def search_terms(pin: Pin, gateway: MediaProvider) -> list[str]:
        """Candidate search queries for this pin, most specific first.

        Some search engines (e.g. Wikimedia Commons) return nothing for an
        overly specific query like a full street address, but do match a
        broader name + city/state query -- multi-query providers get a second,
        narrower candidate to widen recall (see ``MediaProvider.get_media``).

        Args:
            pin: The pin to build search queries for.
            gateway: The provider gateway (controls quoting/country flags).

        Returns:
            Ordered, de-duplicated list of query strings; may be empty.
        """
        if gateway.reject_address_derived_names and pin.location is not None:
            from urbanlens.dashboard.services.locations.naming import is_address_derived_name

            fallback_name = pin.meaningful_official_name or pin.meaningful_name
            # A pin with no real landmark name falls back to its raw street
            # address as the "name" - a query built from that has no genuine
            # narrowing power (just a house number and a generic street-type
            # word), so a provider whose relevance ranking treats query words
            # as independent OR terms is skipped entirely rather than fed a
            # guaranteed-noisy query (see LOCJsonGateway).
            if fallback_name and is_address_derived_name(fallback_name, pin.location):
                return []

        search_term = pin.get_unique_search_name(
            include_country=gateway.search_with_country,
            quote_name=gateway.quote_name,
            include_address=gateway.include_address,
            quote_locality=gateway.quote_locality,
        )
        if not search_term:
            return []
        terms = [search_term]
        if gateway.multi_query:
            narrow_term = pin.get_unique_search_name(
                include_country=gateway.search_with_country,
                quote_name=gateway.quote_name,
                include_address=False,
                quote_locality=gateway.quote_locality,
            )
            if narrow_term and narrow_term not in terms:
                terms.append(narrow_term)
        return terms

    def fetch(self, pin: Pin) -> None:
        """Fetch this provider's media; ``get_media`` persists to LocationCache."""
        gateway = self.make_gateway()
        terms = self.search_terms(pin, gateway)
        if not terms:
            from urbanlens.dashboard.models.cache.location_cache import LocationCache

            LocationCache.set(pin.location, self.cache_source, {"items": []}, query_key="")
            return
        gateway.get_media(pin.location, terms)

    def gate(self, pin: Pin) -> bool:
        """Geo-restricted providers and pins with no usable search name are skipped."""
        gateway = self.make_gateway()
        if gateway.geo_boundary is not None and not gateway.geo_boundary.contains(pin.effective_latitude, pin.effective_longitude):
            return False
        return bool(self.search_terms(pin, gateway))

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from this provider's cached ``{"items": [...]}``."""
        return [MediaItem(**item) for item in (data or {}).get("items", [])]


class BoundaryPanelSource(PanelSource):
    """Auto-generated default boundaries stored on the Location's Boundary rows.

    Location-scoped: the generated property/building boundaries are shared
    place data, so one fetch serves every pin (and the wiki page) at that
    Location. This is the lazy path that replaced eager generation on pin
    creation - the provider chain only runs when someone actually views a pin
    detail page (or creates a wiki).
    """

    key = "boundary"
    # Stays on the default (prefork) queue, not the fast thread-pool queue -
    # generate_location_boundaries does real CPU-bound work (gunzipping
    # building-footprint shards, shapely geometry ops), and several of those
    # running concurrently on a thread pool would cause enough GIL contention
    # to slow down every other panel sharing it. See PanelSource.queue.
    queue = "celery"

    def scope(self, pin: Pin) -> str:
        """Location-scoped: default boundaries are keyed by Location."""
        return f"loc{pin.location_id}"

    def is_ready(self, pin: Pin) -> bool:
        """True when the provider chain has run for the pin's Location.

        ``generated_at`` is stamped even when no polygon was found, so a
        fruitless run doesn't retrigger the chain on every page view.
        """
        from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

        if pin.location_id is None:
            return True
        row = Boundary.objects.row_for_location(pin.location, BoundaryType.PROPERTY)
        return row is not None and row.generated_at is not None

    def fetch(self, pin: Pin) -> None:
        """Run the boundary provider chain and persist generated polygons.

        The chain's heavy steps (downloading and gunzipping building-footprint
        shards, shapely geometry work) are exactly why this runs in Celery: on
        the request path that CPU work blocked the entire gevent event loop.
        Persistence uses queryset ``update()`` calls (see
        ``generate_location_boundaries``) so it can never clobber geometry
        saved concurrently by the web request.
        """
        from urbanlens.dashboard.services.locations.boundaries import generate_location_boundaries

        if pin.location_id is None or self.is_ready(pin):
            return
        generate_location_boundaries(pin.location, name=pin.effective_name)


def _satellite_gateways() -> list[SatelliteViewProvider]:
    """The plugin-contributed satellite imagery provider chain, in display order."""
    from urbanlens.dashboard.plugins import plugin_registry

    return plugin_registry.satellite_providers()


def _street_view_gateways() -> list[StreetViewProvider]:
    """The plugin-contributed street-level imagery provider chain, in display order."""
    from urbanlens.dashboard.plugins import plugin_registry

    return plugin_registry.street_view_providers()


def collect_satellite_slides(lat: float, lng: float) -> tuple[list[SatelliteSlide], list[ProviderFetchResult]]:
    """Gather satellite slides from every provider, tolerating per-provider failure.

    Each provider caches its own slides (24h, keyed by coordinates), so
    running this twice is one round of upstream fetches followed by pure cache
    hits -- the Celery warm-up task and the request-path render share this
    exact function.

    Args:
        lat: WGS-84 latitude.
        lng: WGS-84 longitude.

    Returns:
        Tuple of (all slides in provider order, per-provider outcomes for the
        admin debug overlay).
    """
    slides: list[SatelliteSlide] = []
    results: list[ProviderFetchResult] = []
    for gateway in _satellite_gateways():
        service = gateway.service_key or type(gateway).__name__
        try:
            gateway_slides, from_cache = gateway.get_satellite_slides(lat, lng)
            slides.extend(gateway_slides)
            results.append(ProviderFetchResult(service, from_cache=from_cache, count=len(gateway_slides)))
        except RequestCancelledError as rce:
            logger.debug("Satellite view provider %s request cancelled -> %s", service, rce)
        except Exception as e:
            # TODO: Catch specific exceptions
            logger.warning("Satellite view provider %s failed -> %s", service, e)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
    return slides, results


def collect_street_view_slides(lat: float, lng: float) -> tuple[list[StreetViewSlide], list[ProviderFetchResult]]:
    """Gather street-level slides from every provider, tolerating per-provider failure.

    Args:
        lat: WGS-84 latitude.
        lng: WGS-84 longitude.

    Returns:
        Tuple of (all slides in provider order, per-provider outcomes for the
        admin debug overlay).
    """
    slides: list[StreetViewSlide] = []
    results: list[ProviderFetchResult] = []
    for provider in _street_view_gateways():
        service = provider.service_key or type(provider).__name__
        try:
            provider_slides, from_cache = provider.get_street_view_slides(lat, lng)
            slides.extend(provider_slides)
            results.append(ProviderFetchResult(service, from_cache=from_cache, count=len(provider_slides)))
        except RequestCancelledError as rce:
            logger.debug("Street view provider %s request cancelled -> %s", service, rce)
        except Exception:
            # TODO: Catch specific exceptions
            logger.warning("Street view provider %s failed", service, exc_info=True)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
    return slides, results


class SlidesPanelSource(PanelSource, ABC):
    """Base for the satellite/street carousels, whose store is per-provider Django cache.

    The providers each cache their own slides for 24h keyed by coordinates;
    "ready" is tracked with a separate summary marker set after a full
    warm-up pass, whose TTL is deliberately shorter than the slide caches so
    the marker always lapses (triggering a background re-warm) before the
    underlying entries can expire mid-render.
    """

    def scope(self, pin: Pin) -> str:
        """Coordinate-scoped, matching the providers' own cache keys."""
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        return f"{lat:.5f},{lng:.5f}"

    def ready_key(self, pin: Pin) -> str:
        """Cache key of the "provider caches are warm" summary marker."""
        return f"ulfetch:ready:{self.key}:{self.scope(pin)}"

    def is_ready(self, pin: Pin) -> bool:
        """True when a warm-up pass has completed for these coordinates."""
        return bool(cache.get(self.ready_key(pin)))

    @abstractmethod
    def collect(self, lat: float, lng: float) -> tuple[list, list[ProviderFetchResult]]:
        """Run this carousel's provider chain (see the module-level collectors)."""

    def fetch(self, pin: Pin) -> None:
        """Warm every provider's slide cache, then set the ready marker."""
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        self.collect(lat, lng)
        cache.set(self.ready_key(pin), 1, SLIDES_READY_TTL_SECONDS)


class SatellitePanelSource(SlidesPanelSource):
    """Multi-provider satellite imagery carousel."""

    key = "satellite"
    section_id = "satellite-view-section"
    icon = "globe"
    title = "Satellite View"
    outer_class = "satellite-view card card--primary"
    outer_is_card = True

    def collect(self, lat: float, lng: float) -> tuple[list[SatelliteSlide], list[ProviderFetchResult]]:
        """Run the satellite provider chain."""
        return collect_satellite_slides(lat, lng)


class StreetViewPanelSource(SlidesPanelSource):
    """Multi-provider street-level imagery carousel."""

    key = "street_view"
    section_id = "street-view-section"
    icon = "streetview"
    title = "Street View"
    outer_class = "street-view card card--primary"
    outer_is_card = True

    def collect(self, lat: float, lng: float) -> tuple[list[StreetViewSlide], list[ProviderFetchResult]]:
        """Run the street-view provider chain."""
        return collect_street_view_slides(lat, lng)


#: Panels that belong to the core application rather than any one plugin:
#: the default boundaries and the two imagery carousels (which aggregate
#: plugin-contributed providers but are themselves core features).
_CORE_PANEL_SOURCES: tuple[PanelSource, ...] = (
    BoundaryPanelSource(),
    SatellitePanelSource(),
    StreetViewPanelSource(),
)


def panel_sources() -> dict[str, PanelSource]:
    """Every registered panel source, keyed by the source key used in URLs,
    Celery task arguments, and cache keys.

    Combines the core sources with the contributions of every enabled plugin.
    A plugin source whose key collides with an existing one is logged and
    skipped.

    Returns:
        Mapping of source key to its :class:`PanelSource`.
    """
    from urbanlens.dashboard.plugins import plugin_registry

    sources: dict[str, PanelSource] = {source.key: source for source in _CORE_PANEL_SOURCES}
    for source in plugin_registry.panel_sources():
        if source.key in sources:
            logger.warning("Ignoring duplicate panel source '%s' from plugins", source.key)
            continue
        sources[source.key] = source
    return sources


def get_panel_source(source_key: str) -> PanelSource | None:
    """Look up one panel source by key.

    Args:
        source_key: A :func:`panel_sources` key.

    Returns:
        The panel source, or None when no core panel or enabled plugin
        provides that key.
    """
    return panel_sources().get(source_key)


def schedule_panel_fetch(source_key: str, pin: Pin) -> bool:
    """Ensure a background fetch is in flight for this panel, single-flight.

    Args:
        source_key: A :func:`panel_sources` key.
        pin: The pin whose panel data should be fetched.

    Returns:
        True when a fetch is in flight (newly scheduled or already running) --
        the caller should return a polling placeholder. False when the source
        is unknown (e.g. its plugin was disabled) or currently suppressed
        after a failure or disable -- the caller should give up quietly (204).
    """
    source = get_panel_source(source_key)
    if source is None:
        logger.warning("schedule_panel_fetch: unknown source '%s' for pin %s", source_key, getattr(pin, "pk", None))
        return False
    if not pin.profile.external_apis_enabled:
        return False
    if cache.get(source.skip_key(pin)):
        logger.debug("schedule_panel_fetch: %s for pin %s is suppressed, skipping", source_key, pin.pk)
        return False
    if cache.add(source.flight_key(pin), 1, FLIGHT_TTL_SECONDS):
        from urbanlens.dashboard.tasks import fetch_panel_source

        logger.debug("schedule_panel_fetch: dispatching %s for pin %s to queue '%s'", source_key, pin.pk, source.queue)
        fetch_panel_source.apply_async(args=[source_key, pin.pk], queue=source.queue)
    return True


def run_panel_fetch(source_key: str, pin: Pin) -> None:
    """Execute one panel fetch inside the Celery worker.

    Owns the failure policy so individual sources don't have to:

    * Success clears the single-flight marker; the next poll renders.
    * A rate-limit or service-disabled signal suppresses the source for
      :data:`DISABLED_SKIP_TTL_SECONDS` -- polls stop immediately and the
      panel stays quietly absent until the marker lapses.
    * Any other failure suppresses for :data:`FAILURE_SKIP_TTL_SECONDS`, so a
      broken provider degrades to an absent panel instead of being retried by
      every page load's poll cycle.

    Args:
        source_key: A :func:`panel_sources` key.
        pin: The pin whose panel data should be fetched.
    """
    source = get_panel_source(source_key)
    if source is None:
        logger.warning("Panel fetch for unknown source '%s' skipped (plugin removed or disabled?)", source_key)
        return
    if not pin.profile.external_apis_enabled:
        # External APIs may have been turned off after this task was enqueued;
        # skip without recording a failure so the panel just stays absent.
        cache.delete(source.flight_key(pin))
        return

    started = time.monotonic()
    logger.debug("Panel fetch %s for pin %s starting on queue '%s'", source_key, pin.pk, source.queue)
    try:
        source.fetch(pin)
    except (RateLimitExceededError, ServiceDisabledError) as exc:
        logger.debug("Panel fetch %s for pin %s skipped: %s", source_key, pin.pk, exc)
        cache.set(source.skip_key(pin), 1, DISABLED_SKIP_TTL_SECONDS)
    except SoftTimeLimitExceeded:
        # Celery's own worker log already recorded the soft time limit at
        # WARNING with full task context; a second ERROR-level traceback here
        # would just be noise for the same event. Suppress like any other
        # failure and let the task end - re-raising would still hit the hard
        # time limit before doing anything useful with the remaining budget.
        logger.warning(
            "Panel fetch %s for pin %s hit its soft time limit after %.1fs; suppressing for %ss",
            source_key,
            pin.pk,
            time.monotonic() - started,
            FAILURE_SKIP_TTL_SECONDS,
        )
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    except Exception:
        logger.exception("Panel fetch %s for pin %s failed after %.1fs", source_key, pin.pk, time.monotonic() - started)
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    else:
        logger.debug("Panel fetch %s for pin %s finished in %.1fs", source_key, pin.pk, time.monotonic() - started)
    finally:
        cache.delete(source.flight_key(pin))
