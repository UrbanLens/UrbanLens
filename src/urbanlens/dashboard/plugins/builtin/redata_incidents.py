"""Police incidents plugin: block-scale incident reports near a pin, via REData.

Safety context for visiting a site, from nine cities' own open-data portals.
Two source properties the rendering respects rather than papers over: every
publisher fuzzes locations before release (a point is *not* evidence about a
specific building - the panel says "this block", never "this address"), and
comparing counts across cities compares publishing scope, so the panel never
ranks and always names the window it covers.
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

_MAX_ROWS = 6
_YEARS = 3


class PoliceIncidentsPanelSource(CoordinateGatedInfoPanelSource):
    """Reported police incidents within the pin's block (500 m, publisher-pinned)."""

    key = "redata_incidents"
    cache_source = "redata_incidents"
    section_id = "police-incidents-section"
    icon = "local_police"
    title = "Reported Incidents"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self, pin: Pin) -> bool:
        """Requires US coordinates (providers are US cities) and REData to be configured."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Search the covering city's incident feed via REData and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import RedataIncidentsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataIncidentsGateway().get_incidents(lat, lng, years=_YEARS, limit=50)
        LocationCache.set(pin.location, self.cache_source, {"incidents": envelope.results}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Summarize by category, then list the most recent incidents."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import INCIDENT_CATEGORY_LABELS

        incidents = (data or {}).get("incidents") or []
        # Traffic collisions are police *reports* rather than crime, and only
        # some feeds publish any - including them would make the same block
        # look "worse" in one city than another purely by publishing scope.
        incidents = [incident for incident in incidents if incident.get("category") != "traffic"]
        if not incidents:
            return None

        by_category: dict[str, int] = {}
        for incident in incidents:
            category = incident.get("category") or "other"
            by_category[category] = by_category.get(category, 0) + 1
        top = sorted(by_category.items(), key=lambda item: -item[1])[:3]
        chips = [f"{len(incidents)} on this block in {_YEARS} years"]
        chips.extend(f"{count}x {INCIDENT_CATEGORY_LABELS.get(category, category)}" for category, count in top)

        meta = []
        for incident in incidents[:_MAX_ROWS]:
            occurred = (incident.get("occurred_at") or "")[:10]
            category = INCIDENT_CATEGORY_LABELS.get(incident.get("category") or "other", "Other")
            description = (incident.get("offense_description") or "").strip().capitalize()
            meta.append({"label": occurred or "Undated", "value": f"{category}" + (f" - {description}" if description else "")})

        # The publishers fuzz coordinates to block scale before release; say so
        # rather than letting the panel imply address-level knowledge.
        meta.append({"label": "Precision", "value": "Locations are approximate (block scale, as published)"})

        return {"chips": chips, "meta": meta}

    def debug_count(self, data: dict) -> int:
        """Number of incidents found (before the traffic filter)."""
        return len((data or {}).get("incidents") or [])


class PoliceIncidentsPlugin(UrbanLensPlugin):
    """Block-scale reported-incident context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_incidents"
    verbose_name: ClassVar[str] = "Reported Police Incidents"
    description: ClassVar[str] = "Shows recent reported police incidents on the pin's block as visit-safety context, from city open-data portals via REData. Locations are block-scale by publication; traffic collisions are excluded."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the reported-incidents pin-detail panel."""
        return [PoliceIncidentsPanelSource()]
