"""Elevation plugin: metres above sea level, sourced through REData.

Contributes a simple pin-detail "Elevation" info panel; see
``services.apis.locations.redata_elevation_gateway`` for the gateway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Meters-to-feet, for showing both units without requiring a second lookup.
_METERS_PER_FOOT = 0.3048


def _pick_elevation(readings: list[dict[str, Any]]) -> float | None:
    """The single "best" elevation reading to show as one line.

    REData orders ``results`` finest-resolution-first, so the first entry is
    authoritative. Its ``elevation_meters: null`` is itself a real answer (a
    gap in that model's own coverage, e.g. open ocean) - not a cue to fall
    through to a coarser model looking for a non-null value instead.
    """
    if not readings:
        return None
    value = readings[0].get("elevation_meters")
    return float(value) if isinstance(value, (int, float)) else None


class ElevationPanelSource(CoordinateGatedInfoPanelSource):
    """The pin's elevation above (or below) sea level."""

    key = "open_elevation"
    cache_source = "open_elevation"
    section_id = "open-elevation-section"
    icon = "landscape"
    title = "Elevation"

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Look up elevation from every REData-configured DEM and cache the results.

        Letting :class:`~...redata_context_gateway.LocationContextUnavailableError`
        propagate (rather than catching it here) lets ``run_panel_fetch``'s
        shared failure-suppression decide the retry cadence for a REData
        outage instead of this panel permanently caching a null reading for
        what might be transient.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_elevation_gateway import RedataElevationGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataElevationGateway().get_elevation(lat, lng)
        LocationCache.set(pin.location, self.cache_source, {"elevation_m": _pick_elevation(envelope.results), "readings": envelope.results}, query_key=f"{lat:.5f},{lng:.5f}")

    #: A lookup outside every model's coverage caches a row with no reading,
    #: which rendered an empty tab.
    inspects_content = True

    def has_content(self, data: dict | None) -> bool:
        """Mirror of :meth:`render_context`'s own emptiness test.

        Args:
            data: The cached payload.

        Returns:
            True when there is an elevation to show.
        """
        return (data or {}).get("elevation_m") is not None

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build a quick-fact line from the primary reading, plus any other model's reading that disagrees."""
        elevation_m = (data or {}).get("elevation_m")
        if elevation_m is None:
            return None

        elevation_ft = elevation_m / _METERS_PER_FOOT
        below_sea_level = elevation_m < 0
        text = f"{abs(elevation_m):,.0f} m ({abs(elevation_ft):,.0f} ft) {'below' if below_sea_level else 'above'} sea level"
        context: dict[str, Any] = {"facts": [{"icon": self.icon, "text": text}]}

        other_readings = [r for r in (data or {}).get("readings", [])[1:] if isinstance(r.get("elevation_meters"), (int, float))]
        if other_readings:
            context["meta"] = [{"label": (reading.get("dataset") or reading.get("provider") or "Other model"), "value": f"{reading['elevation_meters']:,.0f} m"} for reading in other_readings]
        return context


class OpenElevationPlugin(UrbanLensPlugin):
    """Elevation lookups sourced through REData's pooled digital-elevation-model registry."""

    name: ClassVar[str] = "open_elevation"
    verbose_name: ClassVar[str] = "Elevation"
    description: ClassVar[str] = "Shows the pin's elevation above/below sea level on the pin detail page, sourced through REData (USGS 3DEP, Open-Elevation, Open-Meteo/Copernicus)."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the elevation pin-detail panel."""
        return [ElevationPanelSource()]
