"""National Park Service plugin: nearby-park panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelApiKind, info_card

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: How many of a park's activity tags become chips. NPS lists dozens for a big
#: unit ("Hiking", "Wildlife Watching", "Astronomy", ...); past the first
#: handful they stop characterizing the place and just become a wall of pills.
#: Mirrors the ``|slice:":8"`` the HTML panel applies for the same reason.
_MAX_ACTIVITY_CHIPS = 8


class NpsPanelSource(LocationCachePanelSource):
    """National Park Service information for the pin's location."""

    key = "nps"
    cache_source = "nps"
    section_id = "nps-section"
    icon = "park"
    title = "National Park Service"
    # Bespoke markup on the web (a hero photo, prose, activity chips), but the
    # facts underneath are an ordinary information card, so the API serves it
    # through the same INFO contract every other panel uses rather than
    # inventing an NPS-shaped response only this one plugin's clients know.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO})

    def fetch(self, pin: Pin) -> None:
        """Cache NPS details for the park the pin sits inside, if any.

        The panel is about the pinned place *being in* a national park, so the
        result comes from a boundary-containment check -- not a proximity
        search. When the pin falls outside every NPS unit an empty result is
        cached, which keeps the panel hidden.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        park = NPSGateway().find_park_containing_location(lat, lng)
        query_key = f"{lat:.5f},{lng:.5f}"
        LocationCache.set(location, self.cache_source, park or {}, query_key=query_key)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The containing NPS unit as an information card, or None.

        Mirrors ``PinController.nps_info``'s own emptiness rule: a cached
        payload with no ``fullName`` means the fetch ran and the pin sits
        outside every NPS unit, which is a settled "nothing here" rather than
        a pending state - the web panel 204s on it and the API omits it.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            ``{"info": {...}}``, or None when nothing has landed yet or the
            pin is not inside a park.
        """
        data = self.cached_data(pin)
        if not data or not data.get("fullName"):
            return None

        park_url = data.get("url") or ""
        images = data.get("images") or []
        first_image = images[0] if isinstance(images, list) and images else {}
        meta = [{"label": label, "value": data[key]} for key, label in (("designation", "Designation"), ("states", "States"), ("parkCode", "Park Code")) if data.get(key)]

        return {
            PanelApiKind.INFO.value: info_card(
                heading_name=data.get("fullName"),
                chips=[activity.get("name") for activity in (data.get("activities") or [])[:_MAX_ACTIVITY_CHIPS] if isinstance(activity, dict)],
                meta=meta,
                header_link={"url": park_url, "label": "View on NPS.gov"} if park_url else None,
                footer_link={"url": park_url, "label": "View on NPS.gov"} if park_url else None,
                image_url=first_image.get("url") if isinstance(first_image, dict) else None,
                description=data.get("description"),
            ),
        }


class NpsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the containing-national-park cache (a name/alias source) per Location."""

    key: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    cache_source: ClassVar[str] = "nps"
    service_keys: ClassVar[tuple[str, ...]] = ("nps",)
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self) -> bool:
        """Requires the NPS API key."""
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        return bool(app_settings.nps_api_key)

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Look up the NPS unit containing a location, if any.

        Args:
            location: The location to check.

        Returns:
            Tuple of (park payload or None, coordinate query key).
        """
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        park = NPSGateway().find_park_containing_location(lat, lng)
        return park, f"{lat:.5f},{lng:.5f}"


class NpsPlugin(UrbanLensPlugin):
    """National Park Service information for pinned locations."""

    name: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    description: ClassVar[str] = "Shows nearby US national park information on the pin detail page. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the NPS API."""
        return {
            "nps": ServiceDefaults(
                display_name="National Park Service API",
                calls_per_minute=10,
                calls_per_day=500,
                usa_only=True,
                notes="Free API. USA only - NPS covers US national parks exclusively.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the NPS pin-detail panel."""
        return [NpsPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the containing park's name as a place-name candidate."""
        return [LocationCacheNameProvider(source="nps", cache_source="nps", keys=("fullName", "name"), verbose_name="National Park Service")]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the containing-park cache to scheduled background enrichment."""
        return [NpsEnrichmentSource()]
