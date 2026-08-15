"""Underground structures plugin: mapped subsurface features near a pin, via REData.

Tunnels, culverts, station levels, access shafts and buried utility runs from
OpenStreetMap - core context for urban exploration. Worldwide but
volunteer-mapped: an empty answer means "nothing mapped here", never
"nothing there", so the panel simply hides rather than asserting absence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Show at most this many structures in the panel's meta grid.
_MAX_ROWS = 8


class UndergroundPanelSource(CoordinateGatedInfoPanelSource):
    """Mapped subsurface structures within 250 m of the pin."""

    key = "redata_underground"
    cache_source = "redata_underground"
    section_id = "underground-section"
    icon = "subway"
    title = "Underground Structures"

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Search REData's subsurface registry near the pin and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_underground_gateway import RedataUndergroundGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataUndergroundGateway().get_underground_structures(lat, lng, limit=25)
        LocationCache.set(pin.location, self.cache_source, {"structures": envelope.results}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the structure list, enterable features first."""
        from urbanlens.dashboard.services.apis.locations.redata_underground_gateway import UNDERGROUND_KIND_LABELS

        structures = (data or {}).get("structures") or []
        if not structures:
            return None

        enterable = [s for s in structures if s.get("is_enterable")]
        chips = [f"{len(structures)} mapped within 250 m"]
        if enterable:
            chips.append(f"{len(enterable)} enterable")

        meta = []
        for structure in (enterable + [s for s in structures if not s.get("is_enterable")])[:_MAX_ROWS]:
            kind = structure.get("kind") or "other"
            label = UNDERGROUND_KIND_LABELS.get(kind, "Underground structure")
            attributes = structure.get("attributes") or {}
            details = [structure.get("name") or ""]
            # OSM's relative stacking order - not a depth in metres, so it is
            # rendered as "level", never with a unit.
            if structure.get("layer") is not None:
                details.append(f"level {structure['layer']}")
            if any(str(k).startswith(("disused", "abandoned")) for k in attributes):
                details.append("disused/abandoned")
            meta.append({"label": label, "value": ", ".join(part for part in details if part) or label})

        return {"chips": chips, "meta": meta, "nested": True}

    def debug_count(self, data: dict) -> int:
        """Number of subsurface structures found."""
        return len((data or {}).get("structures") or [])


class UndergroundPlugin(UrbanLensPlugin):
    """Mapped subsurface structures near pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_underground"
    verbose_name: ClassVar[str] = "Underground Structures"
    description: ClassVar[str] = "Shows OSM-mapped tunnels, culverts, station levels, shafts and buried utility runs near the pin on the detail page, sourced through REData's subsurface-structures registry."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the underground-structures pin-detail panel."""
        return [UndergroundPanelSource()]
