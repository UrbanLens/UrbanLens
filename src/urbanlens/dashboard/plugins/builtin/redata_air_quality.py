"""Air quality plugin: modelled and sensor readings near a pin, via REData."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import PanelPlacement
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class AirQualityPanelSource(RedataInfoPanelSource):
    """Current air-quality readings for the pin's location."""

    key = "redata_air_quality"
    cache_source = "redata_air_quality"
    site_level: ClassVar[bool] = True
    section_id = "air-quality-section"
    icon = "air"
    title = "Air Quality"
    placement: ClassVar[PanelPlacement] = PanelPlacement.REGIONAL
    tab_label: ClassVar[str] = "Air Quality"
    tab_order: ClassVar[int] = 60

    payload_key: ClassVar[str] = "readings"
    row_limit: ClassVar[int | None] = 20
    #: Concentrations change hourly, and REData itself keeps a reading 1-3 hours.
    cache_max_age: ClassVar[timedelta | None] = timedelta(hours=1)

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Current modelled readings plus nearby community sensors."""
        from urbanlens.dashboard.services.apis.locations.redata_air_quality_gateway import RedataAirQualityGateway

        return RedataAirQualityGateway().get_air_quality(latitude, longitude, limit=self.row_limit)

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
            floor = "+" if self.is_full(readings) else ""
            chips.append(f"{len(sensors)}{floor} community sensor{'s' if len(sensors) != 1 else ''} within 5 km")

        if not facts and not sensors:
            return None
        context: dict = {"facts": facts, "chips": chips}
        if observed := _observed_label([modelled] if modelled else sensors):
            context["meta"] = [{"label": "Observed", "value": observed}]
        return context


def _observed_label(readings: list[dict]) -> str:
    """When the newest of ``readings`` was taken, for the card.

    Args:
        readings: REData air-quality rows, each with an ISO 8601 ``observed_at``.

    Returns:
        Such as ``"5 Oct 2026, 14:00 UTC"``, or ``""`` when none carries a readable time.
    """
    times = []
    for reading in readings:
        try:
            observed = datetime.fromisoformat(str(reading.get("observed_at") or ""))
        except ValueError:
            continue
        times.append(observed if observed.tzinfo else observed.replace(tzinfo=UTC))
    if not times:
        return ""
    newest = max(times).astimezone(UTC)
    return f"{newest.day} {newest:%b %Y, %H:%M} UTC"


class AirQualityPlugin(UrbanLensPlugin):
    """Air-quality context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_air_quality"
    verbose_name: ClassVar[str] = "Air Quality"
    description: ClassVar[str] = "Shows current modelled air quality (and a count of nearby community sensors) for the pin's location on the detail page, sourced through REData."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for redata_air_quality."""
        return {
            "redata_air_quality": ServiceDefaults(
                display_name="REData Air Quality",
                calls_per_minute=20,
                calls_per_day=None,
                notes="Modelled and community-sensor readings via GET /air-quality/. Shares REData's one 1,000/hour lookup pool per key. See services.apis.locations.redata_air_quality_gateway.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the air-quality pin-detail panel."""
        return [AirQualityPanelSource()]
