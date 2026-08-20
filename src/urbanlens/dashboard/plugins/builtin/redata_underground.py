"""Underground structures plugin: mapped subsurface features near a pin, via REData.

Tunnels, culverts, station levels, access shafts and buried utility runs from
OpenStreetMap - core context for urban exploration. Worldwide but
volunteer-mapped: an empty answer means "nothing mapped here", never
"nothing there", so the panel simply hides rather than asserting absence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Show at most this many structures in the panel's meta grid.
_MAX_ROWS = 8


class UndergroundPanelSource(RedataInfoPanelSource):
    """Mapped subsurface structures within 250 m of the pin."""

    key = "redata_underground"
    cache_source = "redata_underground"
    section_id = "underground-section"
    icon = "subway"
    title = "Underground Structures"

    payload_key: ClassVar[str] = "structures"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Tunnels, culverts, shafts and buried utility runs near the pin."""
        from urbanlens.dashboard.services.apis.locations.redata_underground_gateway import RedataUndergroundGateway

        return RedataUndergroundGateway().get_underground_structures(latitude, longitude, limit=25)

    def transform_rows(self, rows: list[dict]) -> list[dict]:
        """Drop each structure's geometry before caching.

        The panel renders names/kinds/flags only; a LineString per tunnel
        segment would bloat the cache row for nothing. A future map-overlay
        consumer should fetch its own geometry rather than reading this cache.
        """
        return [{key: value for key, value in structure.items() if key != "geometry"} for structure in rows]

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

        return {"chips": chips, "meta": meta}


class UndergroundPlugin(UrbanLensPlugin):
    """Mapped subsurface structures near pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_underground"
    verbose_name: ClassVar[str] = "Underground Structures"
    description: ClassVar[str] = "Shows OSM-mapped tunnels, culverts, station levels, shafts and buried utility runs near the pin on the detail page, sourced through REData's subsurface-structures registry."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the underground-structures pin-detail panel."""
        return [UndergroundPanelSource()]
