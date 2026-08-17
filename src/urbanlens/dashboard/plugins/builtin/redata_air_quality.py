"""Air quality plugin: modelled and sensor readings near a pin, via REData.

The modelled row (Copernicus CAMS, worldwide) is the panel's primary answer;
nearby crowdsourced sensors are summarized separately and never averaged
into it - the two kinds are not comparable, and sensors of unknown
calibration disagree wildly (see the endpoint doc's 2.4 vs 83.2 ug/m3
example).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class AirQualityPanelSource(CoordinateGatedInfoPanelSource):
    """Current air-quality readings for the pin's location."""

    key = "redata_air_quality"
    cache_source = "redata_air_quality"
    section_id = "air-quality-section"
    icon = "air"
    title = "Air Quality"

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Fetch current readings via REData and cache them."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_air_quality_gateway import RedataAirQualityGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataAirQualityGateway().get_air_quality(lat, lng, limit=20)
        LocationCache.set(pin.location, self.cache_source, {"readings": envelope.results}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Modelled reading as facts; nearby sensors as a count, never averaged in."""
        readings = (data or {}).get("readings") or []
        modelled = next((reading for reading in readings if reading.get("source_kind") == "modelled"), None)
        sensors = [reading for reading in readings if reading.get("source_kind") == "sensor"]
        if not modelled and not sensors:
            return None

        facts = []
        chips = []
        if modelled:
            us_aqi = modelled.get("us_aqi")
            european_aqi = modelled.get("european_aqi")
            if us_aqi is not None:
                facts.append({"icon": "air", "text": f"US AQI {us_aqi:.0f}"})
            elif european_aqi is not None:
                facts.append({"icon": "air", "text": f"European AQI {european_aqi:.0f}"})
            pm25 = modelled.get("pm2_5")
            if pm25 is not None:
                facts.append({"icon": "grain", "text": f"PM2.5 {pm25:.1f} ug/m3"})
            ozone = modelled.get("ozone")
            if ozone is not None:
                facts.append({"icon": "wb_sunny", "text": f"Ozone {ozone:.0f} ug/m3"})
            if facts:
                chips.append("modelled (CAMS)")
        if sensors:
            # Deliberately a count, not values: volunteer sensors of unknown
            # calibration are not summarizable into one number.
            chips.append(f"{len(sensors)} community sensor{'s' if len(sensors) != 1 else ''} within 5 km")

        if not facts and not sensors:
            return None
        return {"facts": facts, "chips": chips}

    def debug_count(self, data: dict) -> int:
        """Number of readings found across both source kinds."""
        return len((data or {}).get("readings") or [])


class AirQualityPlugin(UrbanLensPlugin):
    """Air-quality context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_air_quality"
    verbose_name: ClassVar[str] = "Air Quality"
    description: ClassVar[str] = "Shows current modelled air quality (and a count of nearby community sensors) for the pin's location on the detail page, sourced through REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the air-quality pin-detail panel."""
        return [AirQualityPanelSource()]
