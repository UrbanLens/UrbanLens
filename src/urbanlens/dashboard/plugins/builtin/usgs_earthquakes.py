"""USGS Earthquake Hazards plugin: nearby seismic activity, sourced through REData."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.pins.external_data import PanelPlacement
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: The one provider in REData's hazards registry this panel reads. Unnamed, REData also runs its wildfire and FEMA
#: providers, applies ``min_magnitude`` to their magnitudes (burned acres), and an outage of either leaves the
#: earthquake answer incomplete.
_EARTHQUAKE_PROVIDER = "usgs_earthquakes"
#: Kept as a filter too: the endpoint pools every hazard kind under one row shape.
_EARTHQUAKE_EVENT_TYPE = "earthquake"


class UsgsEarthquakePanelSource(RedataInfoPanelSource):
    """Recent nearby seismic activity for the pin's location."""

    key = "usgs_earthquakes"
    cache_source = "usgs_earthquakes"
    site_level: ClassVar[bool] = True
    section_id = "usgs-earthquakes-section"
    icon = "vibration"
    title = "Recent Seismic Activity"
    placement: ClassVar[PanelPlacement] = PanelPlacement.REGIONAL
    tab_label: ClassVar[str] = "Seismic"
    tab_order: ClassVar[int] = 30

    payload_key: ClassVar[str] = "events"
    row_limit: ClassVar[int | None] = 10

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Recent magnitude 3+ earthquakes within 100 km, from REData's USGS provider only."""
        from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway

        return RedataHazardsGateway().get_hazard_events(latitude, longitude, radius_meters=100_000, providers=[_EARTHQUAKE_PROVIDER], min_magnitude=3.0, years=10, limit=self.row_limit)

    def transform_rows(self, rows: list[dict]) -> list[dict]:
        """Earthquakes only."""
        return [event for event in rows if isinstance(event, dict) and event.get("event_type") == _EARTHQUAKE_EVENT_TYPE]

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

        return {"chips": [f"{self.counted(events)} in the last 10 years"], "meta": meta}


class UsgsEarthquakePlugin(UrbanLensPlugin):
    """USGS earthquake hazard context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "usgs_earthquakes"
    verbose_name: ClassVar[str] = "USGS Earthquake Hazards"
    description: ClassVar[str] = "Shows recent nearby seismic activity as structural-risk context on the Private Pin page, sourced through REData's natural-hazards registry (USGS FDSN event catalog)."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the USGS earthquake pin-detail panel."""
        return [UsgsEarthquakePanelSource()]
