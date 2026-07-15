"""EPA ECHO plugin: EPA-regulated facility data for pinned locations. USA only.

Two panels share one upstream fetch budget (``_fetch_epa_echo_data``, called
by both panels' ``fetch()`` and writing the same ``LocationCache`` row -
mirrors the Yelp plugin's shared-row trick between its Media-gallery tab and
its own bespoke panel):

- ``EpaEchoDetailPanelSource`` ("epa_echo_detail") - specific-site detail
  card, shown unconditionally (not subscription-gated) whenever a regulated
  facility's own coordinates are close enough to the pin's to plausibly BE
  this pin, not just nearby. This is the integration's primary purpose.
- ``EpaEchoNearbyPanelSource`` ("epa_echo") - the list of nearby regulated
  facilities, folded into the subscription-gated "Nearby Research" tab group
  (see ``PinController._NEARBY_RESEARCH_TABS``) rather than shown to everyone.

Both panels are registered so either one's auto-load/click can populate the
shared cache row first; if a subscriber opens the Nearby Research tab within
the same narrow window the unconditional detail card's own auto-load fetch is
still in flight, both may briefly race to fetch independently (their
Celery-task single-flight keys differ, per-panel) - harmless, since the loser
just overwrites the row with equivalent data, but worth knowing about if EPA's
conservative rate limit ever gets tripped by that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource

_CACHE_SOURCE = "epa_echo"

#: Nearby-search candidates (closest-first is NOT guaranteed - see
#: EpaEchoGateway.get_nearby_facilities) whose Detailed Facility Report gets
#: fetched to look for an exact-site match. Bounded to limit the extra API
#: calls this adds; there's no cheaper way to find the truly closest facility
#: since the nearby-search response has no per-facility longitude to sort by.
_EXACT_MATCH_CANDIDATES = 3
#: A facility whose DFR coordinates are within this distance of the pin's own
#: coordinates is treated as "this facility IS the pin", not just nearby.
_EXACT_MATCH_RADIUS_MILES = 0.1


def _miles_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from urbanlens.dashboard.models.profile.model import _haversine_km

    return _haversine_km((lat1, lng1), (lat2, lng2)) * 0.621371


def _fetch_epa_echo_data(pin: Pin) -> dict[str, Any]:
    """Search EPA ECHO for nearby facilities and look for an exact-site match among the closest few.

    Returns the shape persisted to the shared LocationCache row:
    ``{"facilities": [...], "exact_site": {...} | None}``.
    """
    from urbanlens.dashboard.services.apis.locations.epa_echo import EpaEchoGateway
    from urbanlens.dashboard.services.geo_filter import is_usa_coordinates

    lat = float(pin.effective_latitude or 0)
    lng = float(pin.effective_longitude or 0)
    if not is_usa_coordinates(lat, lng):
        return {"facilities": [], "exact_site": None}

    gateway = EpaEchoGateway()
    facilities = gateway.get_nearby_facilities(lat, lng, radius_miles=0.5, limit=10)

    exact_site = None
    best_distance = _EXACT_MATCH_RADIUS_MILES
    for facility in facilities[:_EXACT_MATCH_CANDIDATES]:
        registry_id = facility.get("registry_id") or ""
        if not registry_id:
            continue
        detail = gateway.get_facility_detail(registry_id)
        if not detail or detail.get("latitude") is None or detail.get("longitude") is None:
            continue
        distance = _miles_between(lat, lng, detail["latitude"], detail["longitude"])
        if distance <= best_distance:
            best_distance = distance
            exact_site = {**detail, "registry_id": registry_id, "name": facility.get("name") or "", "address": facility.get("address") or ""}

    return {"facilities": facilities, "exact_site": exact_site}


class EpaEchoNearbyPanelSource(CoordinateGatedInfoPanelSource):
    """List of EPA-regulated facilities near the pin's location (subscription-gated "Nearby Research" tab)."""

    key = "epa_echo"
    cache_source = _CACHE_SOURCE
    section_id = "epa-echo-section"
    icon = "factory"
    title = "EPA Regulated Facilities"

    def fetch(self, pin: Pin) -> None:
        """Fetch and cache nearby-facility + exact-site data (see module docstring)."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        LocationCache.set(pin.location, self.cache_source, _fetch_epa_echo_data(pin), query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the nearby-facility list, excluding the exact-site match (it has its own unconditional card)."""
        facilities = (data or {}).get("facilities") or []
        if not facilities:
            return None

        exact_registry_id = ((data or {}).get("exact_site") or {}).get("registry_id")
        meta = []
        for facility in facilities[:8]:
            if exact_registry_id and facility.get("registry_id") == exact_registry_id:
                continue
            status = facility.get("compliance_status") or "Unknown"
            if facility.get("significant_violator"):
                status = f"{status} (significant violator)"
            meta.append({"label": facility.get("name") or "Unnamed facility", "value": f"{facility.get('address') or ''} - {status}".strip(" -")})

        if not meta:
            return None

        return {
            "chips": [f"{len(meta)} nearby"],
            "meta": meta,
            "footer_link": {"url": "https://echo.epa.gov/", "label": "View on EPA ECHO"},
        }

    def debug_count(self, data: dict) -> int:
        """Number of nearby facilities found."""
        return len((data or {}).get("facilities") or [])


class EpaEchoDetailPanelSource(CoordinateGatedInfoPanelSource):
    """Specific-site EPA compliance detail, shown whenever a regulated facility sits at this exact pin.

    Not subscription-gated - this is the integration's primary purpose, as
    opposed to EpaEchoNearbyPanelSource's list of merely-nearby facilities.
    """

    key = "epa_echo_detail"
    cache_source = _CACHE_SOURCE
    section_id = "epa-echo-detail-section"
    icon = "warning"
    title = "EPA Site Details"

    def fetch(self, pin: Pin) -> None:
        """Fetch and cache nearby-facility + exact-site data (see module docstring)."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        LocationCache.set(pin.location, self.cache_source, _fetch_epa_echo_data(pin), query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the exact-site detail card; None (204, hidden) when no facility matched this pin's coordinates."""
        exact_site = (data or {}).get("exact_site")
        if not exact_site:
            return None

        facts = []
        danger_programs = []
        for program in exact_site.get("programs") or []:
            statute = program.get("statute") or "Program"
            penalties = program.get("total_penalties") or "$0"
            formal_actions = program.get("formal_actions") or "0"
            last_inspection = program.get("last_inspection") or "no recorded inspection"
            facts.append(
                {
                    "icon": "gavel",
                    "text": f"{statute}: {formal_actions} formal enforcement action(s), {penalties} in penalties - last inspected {last_inspection}",
                },
            )
            if program.get("quarters_in_significant_noncompliance") not in (None, "", "0"):
                danger_programs.append(statute)

        meta = [{"label": "Address", "value": exact_site.get("address") or "Unknown"}]
        if danger_programs:
            meta.append({"label": "Significant noncompliance", "value": ", ".join(danger_programs)})

        registry_id = exact_site.get("registry_id")
        footer_link = (
            {"url": f"https://echo.epa.gov/detailed-facility-report?fid={registry_id}", "label": "View full EPA compliance report"}
            if registry_id
            else {"url": "https://echo.epa.gov/", "label": "View on EPA ECHO"}
        )

        return {
            "heading_name": exact_site.get("name") or "EPA-regulated facility",
            "chips": ["Significant noncompliance"] if danger_programs else [],
            "facts": facts,
            "meta": meta,
            "footer_link": footer_link,
        }


class EpaFacilityNameProvider(NameProvider):
    """Suggests the exact-site EPA facility's name as an official-name candidate.

    Only fires when a facility was matched as genuinely AT this pin's
    coordinates (see ``_fetch_epa_echo_data``'s exact-match check) - never
    suggests the name of a merely-nearby facility.
    """

    def __init__(self) -> None:
        """Initialize with the ``epa_echo`` source slug."""
        super().__init__(source="epa_echo", verbose_name="EPA ECHO")

    def candidates(self, location: Location) -> list[str | None]:
        """Return the exact-site facility's name, when one was matched.

        Args:
            location: The location to name.

        Returns:
            A single-item list with the facility name, or empty when no
            exact-site match exists yet (or ever).
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cache_row = LocationCache.get_fresh(location, _CACHE_SOURCE)
        if not cache_row:
            return []
        exact_site = (cache_row.data or {}).get("exact_site")
        if not exact_site:
            return []
        return [exact_site.get("name") or None]


class EpaEchoPlugin(UrbanLensPlugin):
    """EPA ECHO regulated-facility compliance data for pinned locations. USA only."""

    name: ClassVar[str] = "epa_echo"
    verbose_name: ClassVar[str] = "EPA ECHO"
    description: ClassVar[str] = (
        "Free, keyless EPA Enforcement and Compliance History Online (ECHO) lookup - shows an unconditional "
        "compliance detail card when a regulated facility sits at this exact pin, plus a subscription-gated "
        "Nearby Research tab listing nearby facilities and their compliance/violation status. USA only; strong "
        "urbex signal for industrial and contaminated sites."
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
        """Contribute the exact-site detail card and the nearby-facilities list."""
        return [EpaEchoDetailPanelSource(), EpaEchoNearbyPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the exact-site EPA facility name as an official-name candidate."""
        return [EpaFacilityNameProvider()]
