"""Fire & disaster history plugin: wildfires and federal disaster declarations near a pin, via REData.

The same ``/hazards/`` endpoint the seismic panel reads, but the other two
providers: ``nifc_wildfires`` (mapped US fire perimeters back to ~1900 -
whether fire reached this property, not whether the region burns) and
``fema_disasters`` (county-level declarations since 1953, with which
assistance programmes were actually authorised). For a site's back-story,
"burned in 1988" and "flood-declared county, 2011" are often the answer to
"why is this place abandoned".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.pins.external_data import PanelSource

_MAX_ROWS = 7

#: Providers this panel reads; earthquakes stay on their own panel.
_PROVIDERS = ("nifc_wildfires", "fema_disasters")


class HazardHistoryPanelSource(CoordinateGatedInfoPanelSource):
    """Wildfire perimeters and federal disaster declarations for the pin's site."""

    key = "hazard_history"
    cache_source = "hazard_history"
    section_id = "hazard-history-section"
    icon = "local_fire_department"
    title = "Fire & Disaster History"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self, pin: Pin) -> bool:
        """Requires US coordinates (both providers are US-only) and REData to be configured."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Search REData's hazards registry for fire/disaster history and cache it.

        No ``radius_meters``: each provider's own default is the point -
        wildfires tight (2 km: did fire reach *this* property), FEMA fixed
        (declarations designate whole counties). ``years=80`` reaches back
        to FEMA's 1953 start; the wildfire perimeters simply extend further.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataHazardsGateway().get_hazard_events(lat, lng, providers=list(_PROVIDERS), years=80, limit=40)
        # Belt-and-braces: ?provider= already restricts which sources run.
        events = [event for event in envelope.results if event.get("provider") in _PROVIDERS]
        LocationCache.set(pin.location, self.cache_source, {"events": events}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Fires first (they answer a tighter question), then declarations, newest first."""
        events = (data or {}).get("events") or []
        if not events:
            return None

        fires = [event for event in events if event.get("provider") == "nifc_wildfires"]
        declarations = [event for event in events if event.get("provider") == "fema_disasters"]

        chips = []
        if fires:
            chips.append(f"{len(fires)} wildfire{'s' if len(fires) != 1 else ''} reached within 2 km")
        if declarations:
            chips.append(f"{len(declarations)} federal disaster declaration{'s' if len(declarations) != 1 else ''} for this county")

        def newest_first(rows: list[dict]) -> list[dict]:
            return sorted(rows, key=lambda event: event.get("occurred_at") or "", reverse=True)

        meta = []
        for event in newest_first(fires)[: _MAX_ROWS // 2 + 1]:
            year = (event.get("occurred_at") or "")[:4]
            magnitude = event.get("magnitude")
            # NIFC publishes acres burned as the magnitude (scale acres_burned)
            # and 1 January of the fire year - year precision is the source's own.
            size = f"{magnitude:,.0f} acres" if isinstance(magnitude, (int, float)) else "size unrecorded"
            meta.append({"label": f"Wildfire {year or '?'}", "value": f"{event.get('place') or 'Unnamed fire'} ({size})", "href": event.get("url") or ""})

        for event in newest_first(declarations)[: _MAX_ROWS - len(meta)]:
            attributes = event.get("attributes") or {}
            year = (event.get("occurred_at") or "")[:4]
            programs = attributes.get("programs") or []
            detail = attributes.get("designated_area") or event.get("place") or ""
            if programs:
                # "Declared" and "assistance was actually available" are
                # different answers; name the authorised programmes.
                detail = f"{detail} ({', '.join(str(program) for program in programs)})" if detail else ", ".join(str(program) for program in programs)
            label_kind = (event.get("event_type") or "disaster").replace("_", " ")
            meta.append({"label": f"{label_kind.capitalize()} {year or '?'}", "value": detail or "Federal disaster declaration", "href": event.get("url") or ""})

        return {"chips": chips, "meta": meta}

    def debug_count(self, data: dict) -> int:
        """Number of fire/disaster events cached."""
        return len((data or {}).get("events") or [])


class HazardHistoryPlugin(UrbanLensPlugin):
    """Wildfire and federal-disaster history for pinned locations, sourced through REData."""

    name: ClassVar[str] = "hazard_history"
    verbose_name: ClassVar[str] = "Fire & Disaster History"
    description: ClassVar[str] = "Shows NIFC wildfire perimeters that reached the pin's site and FEMA disaster declarations for its county on the detail page, sourced through REData's natural-hazards registry. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the fire & disaster history pin-detail panel."""
        return [HazardHistoryPanelSource()]
