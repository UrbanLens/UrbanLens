"""Background-fetch orchestration for the pin detail page's external-data panels.

Every external-data panel (Wikipedia, media archives, satellite imagery,
campus boundary, ...) used to fetch its upstream data inside the HTTP request
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

Adding a new panel means writing one ``PanelSource`` subclass, registering it
in :data:`PANEL_SOURCES`, and pointing a template fragment at a controller
that follows the ready-render-or-schedule pattern -- the task plumbing,
deduplication, and failure handling are shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, ClassVar

from django.core.cache import cache
from django.utils import timezone

from urbanlens.dashboard.services.apis.locations.bing_maps import BingMapsGateway
from urbanlens.dashboard.services.apis.locations.esri import EsriGateway
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.apis.locations.kartaview import KartaViewGateway
from urbanlens.dashboard.services.apis.locations.mapbox import MapboxGateway
from urbanlens.dashboard.services.apis.locations.mapillary import MapillaryGateway
from urbanlens.dashboard.services.apis.locations.nasa_gibs import NasaGibsGateway
from urbanlens.dashboard.services.apis.locations.open_aerial_map import OpenAerialMapGateway
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError, RequestCancelledError, ServiceDisabledError
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaProvider
    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide

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
    """

    key: ClassVar[str]
    section_id: ClassVar[str] = ""
    icon: ClassVar[str] = "public"
    title: ClassVar[str] = ""
    outer_class: ClassVar[str] = ""
    outer_is_card: ClassVar[bool] = False

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
        persist their own results (LocationCache row, Campus column, Django
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


class WikipediaPanelSource(LocationCachePanelSource):
    """Wikipedia article summary for the pin's location."""

    key = "wikipedia"
    cache_source = "wikipedia"
    section_id = "wikipedia-section"
    icon = "menu_book"
    title = "Wikipedia"

    def fetch(self, pin: Pin) -> None:
        """Find and cache the best-matching Wikipedia article."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        address_components = {
            "locality": location.locality or "",
            "route": location.route or "",
            "street_number": location.street_number or "",
            "administrative_area_level_1": location.administrative_area_level_1 or "",
        }
        name = pin.meaningful_official_name or pin.meaningful_name or ""
        address_bits = ", ".join(
            filter(
                None,
                [
                    " ".join(filter(None, [location.street_number, location.route])),
                    location.locality,
                    location.administrative_area_level_1,
                ],
            )
        )
        query_key = f"{name} ({address_bits})" if name and address_bits else name or address_bits or f"{lat:.5f}, {lng:.5f}"
        article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name)
        LocationCache.set(location, self.cache_source, article or {}, query_key=query_key)


class NominatimPanelSource(LocationCachePanelSource):
    """OpenStreetMap Nominatim place metadata for the pin's location."""

    key = "nominatim"
    cache_source = "nominatim"
    section_id = "nominatim-section"
    icon = "map"
    title = "OpenStreetMap"

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates and cache the place metadata."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        place = NominatimGateway().reverse_geocode(lat, lng)
        LocationCache.set(pin.location, self.cache_source, place or {}, query_key=f"{lat},{lng}")


class UsgsTopoPanelSource(LocationCachePanelSource):
    """USGS Historical Topographic Map Collection maps near the pin."""

    key = "usgs_topo"
    cache_source = "usgs_topo"
    section_id = "usgs-topo-section"
    icon = "terrain"
    title = "USGS Historical Topo Maps"

    def fetch(self, pin: Pin) -> None:
        """Query the TNM API for historical topo maps and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.usgs import UsgsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        result = UsgsGateway().historical_topo_maps_for_coordinates(lat, lng, delta=0.01)
        LocationCache.set(pin.location, self.cache_source, result or {}, query_key=f"{lat:.4f},{lng:.4f}")


class NpsPanelSource(LocationCachePanelSource):
    """National Park Service information for the pin's location."""

    key = "nps"
    cache_source = "nps"
    section_id = "nps-section"
    icon = "park"
    title = "National Park Service"

    def fetch(self, pin: Pin) -> None:
        """Find a nearby national park via the NPS API and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

        location = pin.location
        state_code = location.administrative_area_level_1 or ""
        location_name = pin.meaningful_official_name or pin.meaningful_name or ""
        park = NPSGateway().find_park_near_location(
            float(pin.effective_latitude or 0),
            float(pin.effective_longitude or 0),
            state_code=state_code,
            location_name=location_name,
        )
        query_key = f"{location_name} ({state_code})" if location_name else state_code
        LocationCache.set(location, self.cache_source, park or {}, query_key=query_key)


class LoopnetPanelSource(LocationCachePanelSource):
    """LoopNet commercial real-estate listings for the pin's address."""

    key = "loopnet"
    cache_source = "loopnet"
    section_id = "loopnet-section"
    icon = "business_center"
    title = "LoopNet Listings"

    @staticmethod
    def address(pin: Pin) -> str:
        """Street + city + state search address, or ``""`` when insufficient.

        Args:
            pin: The pin whose location's address should be assembled.

        Returns:
            A comma-joined address string; empty when the location lacks a
            street route (LoopNet needs at least street-level precision).
        """
        location = pin.location
        if not location or not location.route:
            return ""
        parts = [
            " ".join(filter(None, [location.street_number, location.route])),
            location.locality or "",
            location.administrative_area_level_1 or "",
        ]
        return ", ".join(p for p in parts if p).strip(", ")

    def fetch(self, pin: Pin) -> None:
        """Search LoopNet for the pin's address and cache the listings."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.real_estate.loopnet import LoopNetGateway

        address = self.address(pin)
        result = LoopNetGateway().search(address) if address else None
        LocationCache.set(pin.location, self.cache_source, result or {}, query_key=address)


class MediaPanelSource(LocationCachePanelSource):
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
        search_term = pin.get_unique_search_name(include_country=gateway.search_with_country, quote_name=gateway.quote_name)
        if not search_term:
            return []
        terms = [search_term]
        if gateway.multi_query:
            narrow_term = pin.get_unique_search_name(include_country=gateway.search_with_country, quote_name=gateway.quote_name, include_address=False)
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


class CampusBoundaryPanelSource(PanelSource):
    """Auto-generated campus boundary stored on the pin's Campus row."""

    key = "campus"

    def scope(self, pin: Pin) -> str:
        """Pin-scoped: campus boundaries are keyed by Pin, not Location."""
        return f"pin{pin.pk}"

    def is_ready(self, pin: Pin) -> bool:
        """True when the pin's Campus row has a generated boundary."""
        from urbanlens.dashboard.models.campus.model import Campus

        return Campus.objects.filter(pin=pin, generated_polygon__isnull=False).exists()

    def fetch(self, pin: Pin) -> None:
        """Run the boundary provider chain and persist the generated polygon.

        The chain's heavy steps (downloading and gunzipping building-footprint
        shards, shapely geometry work) are exactly why this runs in Celery: on
        the request path that CPU work blocked the entire gevent event loop.
        Persistence uses a single-column queryset ``update()`` so it can never
        clobber other Campus fields saved concurrently by the web request.
        """
        from urbanlens.dashboard.models.campus.model import Campus
        from urbanlens.dashboard.services.locations.boundaries import boundary_as_multipolygon

        campus = Campus.objects.filter(pin=pin).first()
        if campus is None or campus.generated_polygon is not None:
            return
        lat = pin.effective_latitude
        lon = pin.effective_longitude
        if lat is None or lon is None:
            return
        poly = boundary_as_multipolygon(float(lat), float(lon), name=pin.effective_name)
        Campus.objects.filter(pk=campus.pk).update(generated_polygon=poly, updated=timezone.now())


def _satellite_gateways() -> list[SatelliteViewProvider]:
    """The satellite imagery provider chain, in display order."""
    return [
        GoogleMapsGateway(api_key=settings.google_unrestricted_api_key or ""),
        EsriGateway(),
        NasaGibsGateway(),
        MapboxGateway(),
        BingMapsGateway(),
        OpenAerialMapGateway(),
    ]


def _street_view_gateways() -> list[StreetViewProvider]:
    """The street-level imagery provider chain, in display order."""
    return [
        GoogleMapsGateway(api_key=settings.google_unrestricted_api_key or ""),
        MapillaryGateway(),
        KartaViewGateway(),
    ]


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


def _media_sources() -> list[MediaPanelSource]:
    """Build the three media-gallery provider sources."""
    from urbanlens.dashboard.services.apis.assets.loc import LOCJsonGateway
    from urbanlens.dashboard.services.apis.assets.smithsonian import SmithsonianGateway
    from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway

    return [
        MediaPanelSource("smithsonian", SmithsonianGateway.service_key, lambda: SmithsonianGateway(api_key=settings.smithsonian_api_key or "")),
        MediaPanelSource("wikimedia", WikimediaGateway.service_key, WikimediaGateway),
        MediaPanelSource("loc", LOCJsonGateway.service_key, LOCJsonGateway),
    ]


#: Registry of every panel source, keyed by the source key used in URLs,
#: Celery task arguments, and cache keys.
PANEL_SOURCES: dict[str, PanelSource] = {
    source.key: source
    for source in [
        WikipediaPanelSource(),
        NominatimPanelSource(),
        UsgsTopoPanelSource(),
        NpsPanelSource(),
        LoopnetPanelSource(),
        CampusBoundaryPanelSource(),
        SatellitePanelSource(),
        StreetViewPanelSource(),
        *_media_sources(),
    ]
}


def schedule_panel_fetch(source_key: str, pin: Pin) -> bool:
    """Ensure a background fetch is in flight for this panel, single-flight.

    Args:
        source_key: A :data:`PANEL_SOURCES` key.
        pin: The pin whose panel data should be fetched.

    Returns:
        True when a fetch is in flight (newly scheduled or already running) --
        the caller should return a polling placeholder. False when the source
        is currently suppressed after a failure or disable -- the caller
        should give up quietly (204).
    """
    source = PANEL_SOURCES[source_key]
    if cache.get(source.skip_key(pin)):
        return False
    if cache.add(source.flight_key(pin), 1, FLIGHT_TTL_SECONDS):
        from urbanlens.dashboard.tasks import fetch_panel_source

        fetch_panel_source.delay(source_key, pin.pk)
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
        source_key: A :data:`PANEL_SOURCES` key.
        pin: The pin whose panel data should be fetched.
    """
    source = PANEL_SOURCES[source_key]
    try:
        source.fetch(pin)
    except (RateLimitExceededError, ServiceDisabledError) as exc:
        logger.info("Panel fetch %s for pin %s skipped: %s", source_key, pin.pk, exc)
        cache.set(source.skip_key(pin), 1, DISABLED_SKIP_TTL_SECONDS)
    except Exception:
        logger.exception("Panel fetch %s for pin %s failed", source_key, pin.pk)
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    finally:
        cache.delete(source.flight_key(pin))
