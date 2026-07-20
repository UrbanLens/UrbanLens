"""LoopNet plugin: commercial real-estate listings panel on the pin detail page.

Infrastructure only for now - see ``docs/redata.md``. The scraping logic this
plugin used to run locally (``services.apis.real_estate.loopnet.LoopNetGateway``)
has been ported into REData, the standalone service that already owns property
records for this app (see ``plugins.builtin.property_records``), but REData
doesn't expose a commercial-listings lookup endpoint yet. Until it does,
:meth:`LoopnetPanelSource.fetch` persists an explicit empty result - the panel
scheduling, single-flight, and failure-suppression machinery
(``services.external_data``) is fully wired and safe to ship this way (it just
never shows data yet), the same stub shape as ``plugins.builtin.cris_buildings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


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
        """Persist an empty result until REData exposes a commercial-listings lookup endpoint.

        TODO: once REData implements LoopNet retrieval (see module docstring),
        replace this body with a call mirroring
        ``RedataGateway.lookup_parcel`` (see ``plugins.builtin.property_records``).
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        address = self.address(pin)
        LocationCache.set(pin.location, self.cache_source, {}, query_key=address)


class LoopnetPlugin(UrbanLensPlugin):
    """LoopNet commercial real-estate listings for pinned locations."""

    name: ClassVar[str] = "loopnet"
    verbose_name: ClassVar[str] = "LoopNet"
    description: ClassVar[str] = "Shows LoopNet commercial real-estate listings for a pin's address on the pin detail page, via REData. USA only."
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() yet - there's no direct upstream call to
    # rate-limit until REData's commercial-listings endpoint (and its service
    # key) exists. See plugins.builtin.cris_buildings for the same pattern.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the LoopNet pin-detail panel."""
        return [LoopnetPanelSource()]
