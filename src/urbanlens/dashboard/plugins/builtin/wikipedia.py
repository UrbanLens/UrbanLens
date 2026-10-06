"""Wikipedia plugin: article summary panel on the Private Pin page."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.gateway import is_source_outage
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import GatewayMediaPanelSource, LocationCachePanelSource
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaProvider
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "wikipedia"

#: OpenStreetMap names some municipalities by their form of government ("Town of Poughkeepsie");
#: article text uses the bare name.
_MUNICIPAL_PREFIX = re.compile(r"^(?:town|city|village|township|borough|municipality)\s+of\s+", re.IGNORECASE)


def public_name_hint(location: Location | None) -> str:
    """The place's public name to match Wikipedia titles against.

    The Location's official name, else its community wiki's name. Never a pin's own name: the match
    lands in a row every viewer of the place shares.

    Args:
        location: The Location being looked up.

    Returns:
        A meaningful public name, or "".
    """
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    if location is None:
        return ""
    if is_meaningful_name(location.official_name):
        return location.official_name
    wiki = Wiki.objects.existing_for_location(location)
    if wiki is not None and is_meaningful_name(wiki.name):
        return wiki.name
    return ""


def match_address_components(location: Location) -> dict[str, str]:
    """Address components for ``WikipediaGateway``'s match check, looked up when the Location has none.

    A pin dropped on bare coordinates has no address when its first lookup runs, and without one
    only a title match can confirm an article. The street address is backfilled first (Google
    Geocoding, once per Location); failing that, OpenStreetMap supplies the municipality.

    Args:
        location: The Location being looked up.

    Returns:
        ``locality``, ``route``, ``street_number`` and ``administrative_area_level_1``, each possibly "".
    """
    if not location.locality:
        _backfill_street_address(location)
    if not location.locality and not location.route:
        from urbanlens.dashboard.services.locations.addresses import ensure_location_address

        # Without Google this is OpenStreetMap's municipality, county and state, kept on the Location for every later lookup.
        try:
            ensure_location_address(location)
        except Exception as exc:
            if not is_source_outage(exc):
                raise
            logger.info("Address lookup unavailable for location %s; matching without it: %s", location.pk, exc)
    components = {
        "locality": location.locality or "",
        "route": location.route or "",
        "street_number": location.street_number or "",
        "administrative_area_level_1": location.administrative_area_level_1 or "",
    }
    if not components["locality"]:
        components["locality"] = _openstreetmap_municipality(location)
    return components


def _backfill_street_address(location: Location) -> None:
    """Run the once-per-Location street-address backfill now, if it can run and has not."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.services.locations.enrichment import AddressEnrichmentSource, self_reported_skip

    source = AddressEnrichmentSource()
    if self_reported_skip(source) is not None or LocationCache.objects.filter(location=location, source=source.marker_source).exists():
        return
    try:
        source.enrich(location)
    except Exception as exc:
        if not is_source_outage(exc):
            raise
        logger.info("Street-address backfill unavailable for location %s; matching without it: %s", location.pk, exc)


def _openstreetmap_municipality(location: Location) -> str:
    """The municipality OpenStreetMap places the Location in, without a "Town of" style prefix."""
    from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

    lat = float(location.latitude or 0)
    lng = float(location.longitude or 0)
    if not lat and not lng:
        return ""
    try:
        admin = NominatimGateway().reverse_geocode_admin(lat, lng)
    except Exception:
        logger.warning("OpenStreetMap municipality lookup failed for location %s", location.pk, exc_info=True)
        return ""
    return _MUNICIPAL_PREFIX.sub("", (admin or {}).get("city") or "").strip()


#: How many containing places the campus fallback searches from (building, then parcel, then site).
_CAMPUS_FALLBACK_DEPTH = 3

#: Largest parcel whose outline may confirm an article by containing it; a vast lot would confirm unrelated ones.
MAX_MATCH_PARCEL_AREA_SQM = 5_000_000.0


def match_outline(location: Location | None) -> MultiPolygon | None:
    """The property outline an article's own coordinates may confirm it by.

    Only a property's page gets one: a building's article is not every article placed on its campus.

    Args:
        location: The Location being looked up.

    Returns:
        The parcel's geometry, or None.
    """
    from urbanlens.dashboard.services.locations.name_tiers import NamingScope, naming_scope
    from urbanlens.dashboard.services.places.scope import parcel_polygon_for_location

    if location is None or naming_scope(location) != NamingScope.PARCEL:
        return None
    parcel = location.place.parcel if location.place_id and location.place is not None else None
    if parcel is None or (parcel.area_sqm or 0) > MAX_MATCH_PARCEL_AREA_SQM:
        return None
    return parcel_polygon_for_location(location)


def store_wikipedia_match(location: Location, article: dict | None, query_key: str) -> None:
    """Cache a lookup's result, keeping an earlier match over a later miss.

    A refresh that finds nothing is more often a transient upstream failure than a deleted article,
    and replacing a match with ``{}`` would strip the name source and article images it feeds.

    Args:
        location: The Location looked up.
        article: The matched article, or None for no match.
        query_key: The query recorded on the row.
    """
    from django.utils import timezone

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if not article:
        kept = LocationCache.objects.filter(location=location, source=_CACHE_SOURCE, data__has_key="title").exclude(data__title="")
        if kept.update(updated=timezone.now()):
            return
    LocationCache.set(location, _CACHE_SOURCE, article or {}, query_key=query_key)


class WikipediaPanelSource(LocationCachePanelSource):
    """Wikipedia article summary for the pin's location."""

    key = "wikipedia"
    cache_source = "wikipedia"
    section_id = "wikipedia-section"
    icon = "menu_book"
    title = "Wikipedia"

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates to search near."""
        return bool(pin.effective_latitude or pin.effective_longitude)

    def fetch(self, pin: Pin) -> None:
        """Find and cache the best-matching Wikipedia article, matching on public data only."""
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        address_components = match_address_components(location)
        name = public_name_hint(location)
        address_bits = ", ".join(
            filter(
                None,
                [
                    " ".join(filter(None, [address_components["street_number"], address_components["route"]])),
                    address_components["locality"],
                    address_components["administrative_area_level_1"],
                ],
            )
        )
        query_key = f"{name} ({address_bits})" if name and address_bits else name or address_bits or f"{lat:.5f}, {lng:.5f}"
        article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name, within=match_outline(location))
        if article is None:
            article = self._campus_article(location)
        store_wikipedia_match(location, article, query_key)
        if article:
            from urbanlens.dashboard.services.wiki.wiki_seed import apply_wikipedia_cover_if_missing, seed_pin_from_cached_wikipedia

            seed_pin_from_cached_wikipedia(pin)
            if pin.cover_photo_id is None:
                apply_wikipedia_cover_if_missing(pin=pin)

    @staticmethod
    def _campus_article(location: Location) -> dict | None:
        """Campus fallback: search again from each place this one stands in, nearest first.

        A building can sit beyond the geosearch radius from the point its campus's article is placed at, and that
        article is what its panel should show. Only public places are walked: the match lands in the row every viewer
        shares, so the owner's own pin nesting has no say in it.

        Args:
            location: The Location whose own search came up empty.

        Returns:
            The first containing place's matched article, or None.
        """
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway
        from urbanlens.dashboard.services.places import lineage
        from urbanlens.dashboard.services.places.ambiguity import representative_locations

        place = location.place if location.place_id else None
        if place is None:
            return None
        for ancestor in representative_locations(lineage.ancestors_of(place)[:_CAMPUS_FALLBACK_DEPTH]):
            lat = float(ancestor.latitude or 0)
            lng = float(ancestor.longitude or 0)
            if not (lat and lng) or ancestor.pk == location.pk:
                continue
            article = WikipediaGateway().get_article_for_location(lat, lng, match_address_components(ancestor), name=public_name_hint(ancestor), within=match_outline(ancestor))
            if article is not None:
                return article
        return None


class WikipediaEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the Wikipedia article cache (a name/alias source) per Location."""

    key: ClassVar[str] = "wikipedia"
    verbose_name: ClassVar[str] = "Wikipedia article"
    cache_source: ClassVar[str] = "wikipedia"
    service_keys: ClassVar[tuple[str, ...]] = ("wikipedia",)

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Find the best-matching Wikipedia article for a location.

        Args:
            location: The location to fetch an article for.

        Returns:
            Tuple of (article payload or None, query key).
        """
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        name = public_name_hint(location)
        article = WikipediaGateway().get_article_for_location(lat, lng, match_address_components(location), name=name, within=match_outline(location))
        return article, name or f"{lat:.5f}, {lng:.5f}"

    def enrich(self, location: Location) -> bool:
        """Fetch and cache the article, keeping an earlier match over a later miss.

        Args:
            location: The location to fetch an article for.

        Returns:
            True, since every attempt completes this source for the location.
        """
        article, query_key = self.fetch(location)
        store_wikipedia_match(location, article, query_key)
        return True


class WikipediaMediaPanelSource(GatewayMediaPanelSource):
    """Media panel backed by the pin's own matched Wikipedia article, not a generic name search - see ``WikipediaMediaGateway``.
    The "search term" ``fetch`` uses is the exact article title from the Wikipedia summary panel's own cache, so this naturally no-ops for any pin without a confidently-matched article - there's nothing to read images from yet."""

    @staticmethod
    def search_terms(pin: Pin, _gateway: MediaProvider) -> list[str]:
        """The matched article's exact title, or ``[]`` if none is cached (yet, or ever)."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if pin.location is None:
            return []
        cached = LocationCache.get_fresh(pin.location, "wikipedia")
        if cached is None:
            return []
        title = (cached.data or {}).get("title") or ""
        return [title] if title else []

    def gate(self, pin: Pin) -> bool:
        """Whether to attempt this panel at all."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if pin.location is None:
            return False
        cached = LocationCache.get_fresh(pin.location, "wikipedia")
        if cached is None:
            return True
        return bool((cached.data or {}).get("title"))

    def fetch(self, pin: Pin) -> None:
        """Fetch this pin's article images, deduped against the Wikimedia Commons panel.

        Nothing is cached until the article lookup has landed: the gate already declines a Location it found no article
        for, so a missing title here means that lookup has not answered yet.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaMediaGateway

        gateway = self.make_gateway()
        terms = self.search_terms(pin, gateway)
        if not terms:
            return
        if isinstance(gateway, WikipediaMediaGateway) and pin.location is not None:
            from urbanlens.dashboard.services.apis.assets.base import MediaItem
            from urbanlens.dashboard.services.media.subject_relevance import subject_for_location

            wikimedia_cache = LocationCache.get_fresh(pin.location, "wikimedia")
            if wikimedia_cache is not None:
                # Only what the Commons tab shows: an article image it leaves out as not about the place stays here.
                subject = subject_for_location(pin.location)
                shown = (MediaItem(**item) for item in (wikimedia_cache.data or {}).get("items", []))
                gateway.known_urls = frozenset(item.url for item in shown if item.url and subject.matches(item))
        gateway.get_media(pin.location, terms)


class WikipediaPlugin(UrbanLensPlugin):
    """Wikipedia article summaries for pinned locations."""

    name: ClassVar[str] = "wikipedia"
    verbose_name: ClassVar[str] = "Wikipedia"
    description: ClassVar[str] = "Shows the best-matching Wikipedia article for a pin's location on the Private Pin page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Wikipedia API."""
        return {
            "wikipedia": ServiceDefaults(
                display_name="Wikipedia",
                category=EgressCategory.QUOTA,
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Free API. Be polite - set a descriptive User-Agent.",
            ),
            "wikipedia_media": ServiceDefaults(
                display_name="Wikipedia (article images)",
                category=EgressCategory.QUOTA,
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free API. Only called for pins with an already-matched article.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Wikipedia summary panel and its article-images Media panel."""
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaMediaGateway

        return [
            WikipediaPanelSource(),
            WikipediaMediaPanelSource("wikipedia_media", WikipediaMediaGateway.service_key, WikipediaMediaGateway),
        ]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the cached article's title as a place-name candidate."""
        return [LocationCacheNameProvider(source="wikipedia", cache_source="wikipedia", keys=("title",), verbose_name="Wikipedia")]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the Wikipedia article cache to scheduled background enrichment."""
        return [WikipediaEnrichmentSource()]
