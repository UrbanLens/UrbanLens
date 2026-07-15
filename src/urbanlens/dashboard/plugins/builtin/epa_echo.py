"""EPA ECHO plugin: nearby regulated-facility compliance panel. USA only."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class EpaEchoPanelSource(CoordinateGatedInfoPanelSource):
    """EPA-regulated facilities and their compliance status near the pin's location."""

    key = "epa_echo"
    cache_source = "epa_echo"
    section_id = "epa-echo-section"
    icon = "factory"
    title = "EPA Regulated Facilities"

    def fetch(self, pin: Pin) -> None:
        """Search EPA ECHO for nearby facilities and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.epa_echo import EpaEchoGateway
        from urbanlens.dashboard.services.geo_filter import is_usa_coordinates

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        if not is_usa_coordinates(lat, lng):
            LocationCache.set(pin.location, self.cache_source, {"facilities": []}, query_key=f"{lat:.5f},{lng:.5f}")
            return

        gateway = EpaEchoGateway()
        facilities = gateway.get_nearby_facilities(lat, lng, radius_miles=0.5, limit=10)

        top_detail = None
        if facilities:
            registry_id = facilities[0].get("registry_id") or ""
            top_detail = gateway.get_facility_detail(registry_id)

        LocationCache.set(pin.location, self.cache_source, {"facilities": facilities, "top_detail": top_detail}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the facility list from EPA ECHO's nearby-facilities search."""
        facilities = (data or {}).get("facilities") or []
        if not facilities:
            return None

        meta = []
        for facility in facilities[:8]:
            status = facility.get("compliance_status") or "Unknown"
            if facility.get("significant_violator"):
                status = f"{status} (significant violator)"
            meta.append({"label": facility.get("name") or "Unnamed facility", "value": f"{facility.get('address') or ''} - {status}".strip(" -")})

        facts = []
        top_detail = (data or {}).get("top_detail")
        if top_detail:
            nearest_name = facilities[0].get("name") or "Nearest facility"
            for program in top_detail.get("programs") or []:
                statute = program.get("statute") or "Program"
                penalties = program.get("total_penalties") or "$0"
                formal_actions = program.get("formal_actions") or "0"
                last_inspection = program.get("last_inspection") or "no recorded inspection"
                facts.append(
                    {
                        "icon": "gavel",
                        "text": f"{nearest_name} ({statute}): {formal_actions} formal enforcement action(s), {penalties} in penalties - last inspected {last_inspection}",
                    }
                )

        return {
            "chips": [f"{len(facilities)} nearby"],
            "facts": facts,
            "meta": meta,
            "footer_link": {"url": "https://echo.epa.gov/", "label": "View on EPA ECHO"},
        }

    def debug_count(self, data: dict) -> int:
        """Number of nearby facilities found."""
        return len((data or {}).get("facilities") or [])


class EpaEchoPlugin(UrbanLensPlugin):
    """EPA ECHO regulated-facility compliance data for pinned locations. USA only."""

    name: ClassVar[str] = "epa_echo"
    verbose_name: ClassVar[str] = "EPA ECHO"
    description: ClassVar[str] = (
        "Free, keyless EPA Enforcement and Compliance History Online (ECHO) lookup - shows nearby "
        "regulated facilities and their compliance/violation status. USA only; strong urbex signal "
        "for industrial and contaminated sites."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the EPA ECHO REST API."""
        return {
            "epa_echo": ServiceDefaults(
                display_name="EPA ECHO",
                calls_per_minute=5,
                calls_per_day=500,
                usa_only=True,
                notes="Free, keyless API; observed to rate-limit aggressively under bursty use - kept conservative.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the EPA ECHO pin-detail panel."""
        return [EpaEchoPanelSource()]
