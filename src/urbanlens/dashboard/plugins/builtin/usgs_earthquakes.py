"""USGS Earthquake Hazards plugin: nearby seismic activity, sourced through REData."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: This panel is specifically "Recent Seismic Activity" - REData's hazards
#: endpoint pools other event kinds (flood, wildfire, ...) behind the same
#: shared registry, so results are filtered to this one even though the only
#: provider configured today (usgs_earthquakes) never returns anything else.
_EARTHQUAKE_EVENT_TYPE = "earthquake"


class UsgsEarthquakePanelSource(CoordinateGatedInfoPanelSource):
    """Recent nearby seismic activity for the pin's location."""

    key = "usgs_earthquakes"
    cache_source = "usgs_earthquakes"
    section_id = "usgs-earthquakes-section"
    icon = "vibration"
    title = "Recent Seismic Activity"

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Search REData's hazards registry for nearby recent earthquakes and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataHazardsGateway().get_hazard_events(lat, lng, radius_meters=100_000, min_magnitude=3.0, years=10, limit=10)
        events = [event for event in envelope.results if event.get("event_type") == _EARTHQUAKE_EVENT_TYPE]
        LocationCache.set(pin.location, self.cache_source, {"events": events}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the seismic-event list from REData's hazards results."""
        events = (data or {}).get("events") or []
        if not events:
            return None

        meta = []
        for event in events[:8]:
            occurred_at = event.get("occurred_at") or ""
            date_label = occurred_at[:10] if occurred_at else "Unknown date"
            magnitude = event.get("magnitude")
            meta.append(
                {
                    "label": f"M{magnitude:.1f}" if isinstance(magnitude, (int, float)) else "Unknown magnitude",
                    # `title`, not `place`: REData normalizes every hazard provider
                    # onto one HazardEvent shape and puts USGS's own `place` string
                    # there. Reading `place` made every row say "Unknown location".
                    "value": f"{event.get('title') or 'Unknown location'} - {date_label}",
                    "href": event.get("url") or "",
                },
            )

        return {"chips": [f"{len(events)} in the last 10 years"], "meta": meta}

    def debug_count(self, data: dict) -> int:
        """Number of seismic events found."""
        return len((data or {}).get("events") or [])


class UsgsEarthquakePlugin(UrbanLensPlugin):
    """USGS earthquake hazard context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "usgs_earthquakes"
    verbose_name: ClassVar[str] = "USGS Earthquake Hazards"
    description: ClassVar[str] = "Shows recent nearby seismic activity as structural-risk context on the Private Pin page, sourced through REData's natural-hazards registry (USGS FDSN event catalog)."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the USGS earthquake pin-detail panel."""
        return [UsgsEarthquakePanelSource()]
