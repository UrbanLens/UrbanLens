"""Wikipedia plugin: article summary panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import LocationCachePanelSource, MediaPanelSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaProvider
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider


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
        if article is None:
            article = self._ancestor_campus_article(pin)
        LocationCache.set(location, self.cache_source, article or {}, query_key=query_key)

    @staticmethod
    def _ancestor_campus_article(pin: Pin) -> dict | None:
        """Campus fallback: search again from each ancestor pin's own point.

        A large campus (an HRSH-style hospital complex, a factory site)
        typically has exactly one Wikipedia article, geotagged at a single
        point - usually the main building. A child pin for an outbuilding can
        easily sit more than the geosearch radius away from that point, so its
        own coordinates find nothing even though the campus article is exactly
        what its panel should show. Rather than widening the global radius
        (which would invite false-positive matches for every ordinary pin),
        each ancestor's coordinates and name get their own normal-radius
        search (UL-354, decision 2026-07-23).

        Args:
            pin: The pin whose own-coordinate search came up empty.

        Returns:
            The first ancestor's matched article dict, or None.
        """
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        seen: set[int] = {pin.pk}
        ancestor = pin.parent_pin
        # Bounded walk: hierarchies are shallow (campus -> building -> spot),
        # and the seen-set guards against a pathological parent cycle.
        for _depth in range(3):
            if ancestor is None or ancestor.pk in seen:
                return None
            seen.add(ancestor.pk)
            lat = float(ancestor.effective_latitude or 0)
            lng = float(ancestor.effective_longitude or 0)
            location = ancestor.location
            if lat and lng and location is not None:
                components = {
                    "locality": location.locality or "",
                    "route": location.route or "",
                    "street_number": location.street_number or "",
                    "administrative_area_level_1": location.administrative_area_level_1 or "",
                }
                name = ancestor.meaningful_official_name or ancestor.meaningful_name or ""
                article = WikipediaGateway().get_article_for_location(lat, lng, components, name=name)
                if article is not None:
                    return article
            ancestor = ancestor.parent_pin
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
        address_components = {
            "locality": location.locality or "",
            "route": location.route or "",
            "street_number": location.street_number or "",
            "administrative_area_level_1": location.administrative_area_level_1 or "",
        }
        name = location.official_name or ""
        article = WikipediaGateway().get_article_for_location(lat, lng, address_components, name=name)
        return article, name or f"{lat:.5f}, {lng:.5f}"


class WikipediaMediaPanelSource(MediaPanelSource):
    """Media panel backed by the pin's own matched Wikipedia article, not a
    generic name search - see ``WikipediaMediaGateway``.

    The "search term" ``fetch`` uses is the exact article title from the
    Wikipedia summary panel's own cache, so this naturally no-ops for any pin
    without a confidently-matched article - there's nothing to read images
    from yet.
    """

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
        """Whether to attempt this panel at all.

        Slightly looser than the base class's "has a search term" check: on a
        pin's very first visit, this panel's own background fetch and the
        Wikipedia summary panel's fetch are scheduled at roughly the same
        time (see ``PanelSource.fetch``'s "runs inside a Celery worker, never
        on the request path" contract - neither can synchronously wait for
        the other on the request path here in ``gate``, which runs on every
        request/poll and must stay cheap). Gating strictly on an
        already-cached title would mean this panel gives up permanently
        (204, the gallery JS's "done, nothing" signal) if it's asked before
        the summary panel has reported back even once - so a pin with no
        ``wikipedia`` LocationCache row *at all* yet (never fetched, as
        opposed to fetched-and-found-nothing) still gets a shot: its own
        fetch will re-check the cache when it actually runs, by which point
        the summary panel has very likely already completed.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if pin.location is None:
            return False
        cached = LocationCache.get_fresh(pin.location, "wikipedia")
        if cached is None:
            return True
        return bool((cached.data or {}).get("title"))

    def fetch(self, pin: Pin) -> None:
        """Fetch this pin's article images, deduped against the Wikimedia Commons panel."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaMediaGateway

        gateway = self.make_gateway()
        terms = self.search_terms(pin, gateway)
        if not terms:
            LocationCache.set(pin.location, self.cache_source, {"items": []}, query_key="")
            return
        if isinstance(gateway, WikipediaMediaGateway) and pin.location is not None:
            wikimedia_cache = LocationCache.get_fresh(pin.location, "wikimedia")
            if wikimedia_cache is not None:
                gateway.known_urls = frozenset(item.get("url", "") for item in (wikimedia_cache.data or {}).get("items", []) if item.get("url"))
        gateway.get_media(pin.location, terms)


class WikipediaPlugin(UrbanLensPlugin):
    """Wikipedia article summaries for pinned locations."""

    name: ClassVar[str] = "wikipedia"
    verbose_name: ClassVar[str] = "Wikipedia"
    description: ClassVar[str] = "Shows the best-matching Wikipedia article for a pin's location on the pin detail page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Wikipedia API."""
        return {
            "wikipedia": ServiceDefaults(
                display_name="Wikipedia",
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Free API. Be polite - set a descriptive User-Agent.",
            ),
            "wikipedia_media": ServiceDefaults(
                display_name="Wikipedia (article images)",
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
