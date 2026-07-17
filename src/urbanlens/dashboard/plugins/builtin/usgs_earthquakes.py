"""USGS Earthquake Hazards plugin: nearby seismic activity panel."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class UsgsEarthquakePanelSource(CoordinateGatedInfoPanelSource):
    """Recent nearby seismic activity for the pin's location."""

    key = "usgs_earthquakes"
    cache_source = "usgs_earthquakes"
    section_id = "usgs-earthquakes-section"
    icon = "vibration"
    title = "Recent Seismic Activity"

    def fetch(self, pin: Pin) -> None:
        """Search USGS for nearby recent earthquakes and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.safety.usgs_earthquakes import UsgsEarthquakeGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        events = UsgsEarthquakeGateway().get_recent_nearby_earthquakes(lat, lng, radius_km=100, min_magnitude=3.0, years=10, limit=10)
        LocationCache.set(pin.location, self.cache_source, {"events": events}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the seismic-event list from USGS's nearby-earthquakes search."""
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
                    "value": f"{event.get('place') or 'Unknown location'} - {date_label}",
                    "href": event.get("url") or "",
                },
            )

        return {"chips": [f"{len(events)} in the last 10 years"], "meta": meta, "nested": True}

    def debug_count(self, data: dict) -> int:
        """Number of seismic events found."""
        return len((data or {}).get("events") or [])


class UsgsEarthquakePlugin(UrbanLensPlugin):
    """USGS earthquake hazard context for pinned locations."""

    name: ClassVar[str] = "usgs_earthquakes"
    verbose_name: ClassVar[str] = "USGS Earthquake Hazards"
    description: ClassVar[str] = (
        "Free, keyless USGS FDSN earthquake catalog lookup - shows recent nearby seismic activity as "
        "structural-risk context on the pin detail page."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the USGS FDSN event API."""
        return {
            "usgs_earthquakes": ServiceDefaults(
                display_name="USGS Earthquake Hazards",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless government API.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the USGS earthquake pin-detail panel."""
        return [UsgsEarthquakePanelSource()]
