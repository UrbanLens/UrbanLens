"""EPA ECHO plugin: EPA-regulated facility data for pinned locations.
Two panels share one upstream fetch budget (``_fetch_epa_echo_data``, called by both panels' ``fetch()`` and writing the same ``LocationCache`` row - mirrors the Yelp plugin's shared-row trick between its Media-gallery tab and its own bespoke panel):"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "epa_echo"

#: A facility whose REData-reported coordinates are within this distance of
#: the pin's own coordinates is treated as "this facility IS the pin", not
#: just nearby.
_EXACT_MATCH_RADIUS_MILES = 0.1


def _miles_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in miles.

    Args:
        lat1: First latitude in degrees.
        lng1: First longitude in degrees.
        lat2: Second latitude in degrees.
        lng2: Second longitude in degrees.

    Returns:
        Distance in miles."""
    from urbanlens.dashboard.models.profile.meta import DistanceUnit
    from urbanlens.dashboard.services.core.units import km_to_display
    from urbanlens.dashboard.services.geo.distance import haversine_km

    return km_to_display(haversine_km(lat1, lng1, lat2, lng2), DistanceUnit.MILES)


def _facility_from_poi(poi: dict[str, Any]) -> dict[str, Any]:
    """Map a REData ``epa_echo`` points-of-interest row onto this plugin's facility shape.
    The compliance data lives in ``attributes``, since the generic ``PointOfInterest`` model promotes only the fields every provider can answer.

    Args:
        poi: One ``PointOfInterestSerializer``-shaped row with ``provider="epa_echo"``.

    Returns:
        ``{"registry_id", "name", "address", "latitude", "longitude", "compliance_status", "significant_violator", "quarters_in_noncompliance", "last_inspection", "inspection_count"}`` - the two ``quarters``/``inspection`` keys keep this plugin's own..."""
    attributes = poi.get("attributes") or {}
    return {
        "registry_id": poi.get("external_id") or "",
        "name": poi.get("name") or "",
        "address": attributes.get("address") or "",
        "latitude": poi.get("latitude"),
        "longitude": poi.get("longitude"),
        "compliance_status": attributes.get("compliance_status") or "",
        "significant_violator": bool(attributes.get("significant_violator")),
        "quarters_in_noncompliance": attributes.get("quarters_with_violation"),
        "last_inspection": attributes.get("last_inspection_date") or "",
        "inspection_count": attributes.get("inspection_count"),
    }


def _fetch_epa_echo_data(pin: Pin) -> dict[str, Any]:
    """Search REData for nearby EPA-regulated facilities and pick out an exact-site match.
    Every facility this function sees is still recorded in ``EpaFacility``, project-wide, exactly as before - reusable by any other pin's own exact-site check, and by :class:`EpaFacilityNameProvider`, without a second REData call."""
    from urbanlens.dashboard.models.epa_facility import EpaFacility
    from urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway import RedataPointsOfInterestGateway
    from urbanlens.dashboard.services.geo.geo_filter import is_usa_coordinates

    lat = float(pin.effective_latitude or 0)
    lng = float(pin.effective_longitude or 0)
    if not is_usa_coordinates(lat, lng):
        return {"facilities": [], "exact_site": None}

    results = RedataPointsOfInterestGateway().find_near(lat, lng, provider="epa_echo")
    facilities = [_facility_from_poi(poi) for poi in results]

    for facility in facilities:
        registry_id = facility.get("registry_id") or ""
        if not registry_id:
            continue
        EpaFacility.record_detail_result(
            registry_id,
            name=facility.get("name") or "",
            address=facility.get("address") or "",
            latitude=facility.get("latitude"),
            longitude=facility.get("longitude"),
            data={k: v for k, v in facility.items() if k not in ("registry_id", "latitude", "longitude")},
        )

    exact_site = None
    best_distance = _EXACT_MATCH_RADIUS_MILES
    for facility in facilities:
        if not facility.get("registry_id"):
            continue
        if facility.get("latitude") is None or facility.get("longitude") is None:
            continue
        distance = _miles_between(lat, lng, facility["latitude"], facility["longitude"])
        if distance <= best_distance:
            best_distance = distance
            exact_site = facility

    return {"facilities": facilities, "exact_site": exact_site}


def _fetch_and_cache(pin: Pin) -> dict[str, Any]:
    """Run the shared upstream fetch, persist the shared cache row, and propagate any exact-site match.

    Args:
        pin: The pin whose location's EPA data should be (re)fetched.

    Returns:
        The freshly-cached payload, so a caller can act on ``exact_site`` without re-reading the cache row."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    lat = float(pin.effective_latitude or 0)
    lng = float(pin.effective_longitude or 0)
    data = _fetch_epa_echo_data(pin)
    LocationCache.set(pin.location, _CACHE_SOURCE, data, query_key=f"{lat:.5f},{lng:.5f}")

    exact_site = data.get("exact_site")
    if exact_site:
        _propagate_exact_site_to_nearby_locations(pin.location, exact_site)
    return data


def _propagate_exact_site_to_nearby_locations(location: Location, exact_site: dict[str, Any]) -> None:
    """Apply a newly-confirmed exact-site EPA match to any other pinned Location within the exact-match radius whose own ``epa_echo`` cache has no match yet.
    Never overwrites a Location that already has its own confirmed ``exact_site`` - only fills in rows that are missing or empty, so a genuinely different real match is never clobbered.

    Args:
        location: The Location the match was just confirmed for (excluded from the neighbor search - it already has the match).
        exact_site: The confirmed exact-site payload, including its own ``latitude``/``longitude``."""
    from django.contrib.gis.geos import Point
    from django.contrib.gis.measure import D

    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location as LocationModel

    site_lat = exact_site.get("latitude")
    site_lng = exact_site.get("longitude")
    if site_lat is None or site_lng is None:
        return

    point = Point(site_lng, site_lat, srid=4326)
    nearby_locations = LocationModel.objects.filter(point__distance_lte=(point, D(mi=_EXACT_MATCH_RADIUS_MILES))).exclude(pk=location.pk).filter(pins__isnull=False).distinct()

    for neighbor in nearby_locations:
        cache_row = LocationCache.objects.filter(location=neighbor, source=_CACHE_SOURCE).first()
        existing_data = cache_row.data if cache_row else {}
        if (existing_data or {}).get("exact_site"):
            continue
        new_data = {**existing_data, "facilities": existing_data.get("facilities") or [], "exact_site": exact_site}
        LocationCache.set(neighbor, _CACHE_SOURCE, new_data, query_key=(cache_row.query_key if cache_row else ""))


class _EpaEchoPanelSourceBase(CoordinateGatedInfoPanelSource):
    """Shared USA + REData-configured gate for both EPA ECHO panel sources."""

    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates within the USA (see ``geo_boundary``) and REData to be configured."""
        return super().gate(pin) and redata_configured()


class EpaEchoNearbyPanelSource(_EpaEchoPanelSourceBase):
    """List of EPA-regulated facilities near the pin's location (subscription-gated "Nearby Research" tab)."""

    key = "epa_echo"
    cache_source = _CACHE_SOURCE
    section_id = "epa-echo-section"
    icon = "factory"
    title = "EPA Regulated Facilities"
    # The subscription gate as a fact about the source rather than only as an entry in a
    # controller's tab dict: any surface that serves this panel - the web tab strip, the external
    # API, whatever comes next - can now check the same field instead of each keeping its own list
    # and eventually disagreeing about which panels are gated.
    required_feature: ClassVar[SiteFeature | None] = SiteFeature.NEARBY_RESEARCH

    def fetch(self, pin: Pin) -> None:
        """Fetch and cache nearby-facility + exact-site data (see module docstring)."""
        _fetch_and_cache(pin)

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
            registry_id = facility.get("registry_id") or ""
            meta.append(
                {
                    "label": facility.get("name") or "Unnamed facility",
                    "value": f"{facility.get('address') or ''} - {status}".strip(" -"),
                    # Links straight to this specific facility's compliance report,
                    # not EPA ECHO's homepage.
                    "href": f"https://echo.epa.gov/detailed-facility-report?fid={registry_id}" if registry_id else "",
                },
            )

        if not meta:
            return None

        return {
            "chips": [f"{len(meta)} nearby"],
            "meta": meta,
        }

    def debug_count(self, data: dict) -> int:
        """Number of nearby facilities found."""
        return len((data or {}).get("facilities") or [])


class EpaEchoDetailPanelSource(_EpaEchoPanelSourceBase):
    """Specific-site EPA compliance detail, shown whenever a regulated facility sits at this exact pin."""

    key = "epa_echo_detail"
    cache_source = _CACHE_SOURCE
    section_id = "epa-echo-detail-section"
    icon = "warning"
    title = "EPA Site Details"

    def fetch(self, pin: Pin) -> None:
        """Fetch and cache nearby-facility + exact-site data (see module docstring)."""
        data = _fetch_and_cache(pin)

        exact_site = data.get("exact_site")
        if exact_site:
            registry_id = exact_site.get("registry_id")
            if registry_id:
                self._add_echo_report_link(pin, pin.location, registry_id)

    @staticmethod
    def _add_echo_report_link(pin: Pin, location: Location, registry_id: str) -> None:
        """Add the EPA ECHO compliance report URL to the pin's (and wiki's) links, if not already there."""
        from urbanlens.dashboard.services.locations.external_links import add_pin_and_wiki_link

        url = f"https://echo.epa.gov/detailed-facility-report?fid={registry_id}"
        add_pin_and_wiki_link(pin, location, url, "EPA Compliance Report")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the exact-site detail card; None (204, hidden) when no facility matched this pin's coordinates."""
        exact_site = (data or {}).get("exact_site")
        if not exact_site:
            return None

        status = exact_site.get("compliance_status") or "Unknown"
        last_inspection = exact_site.get("last_inspection") or "no recorded inspection"
        quarters = exact_site.get("quarters_in_noncompliance")
        fact_text = f"Compliance status: {status} - last inspected {last_inspection}"
        if quarters not in (None, "", "0", 0):
            fact_text += f" ({quarters} quarter(s) in noncompliance)"
        facts = [{"icon": "gavel", "text": fact_text}]

        significant = bool(exact_site.get("significant_violator"))
        meta = [{"label": "Address", "value": exact_site.get("address") or "Unknown"}]
        if significant:
            meta.append({"label": "Significant noncompliance", "value": status})

        registry_id = exact_site.get("registry_id")
        footer_link = (
            # ai_extract: the facility report is a real content page about this
            # exact site, so it offers the AI field-extraction button.
            {"url": f"https://echo.epa.gov/detailed-facility-report?fid={registry_id}", "label": "View full EPA compliance report", "ai_extract": True} if registry_id else {"url": "https://echo.epa.gov/", "label": "View on EPA ECHO"}
        )

        return {
            "heading_name": exact_site.get("name") or "EPA-regulated facility",
            "chips": ["Significant noncompliance"] if significant else [],
            "facts": facts,
            "meta": meta,
            "footer_link": footer_link,
        }


class EpaFacilityNameProvider(NameProvider):
    """Suggests the exact-site EPA facility's name as an official-name candidate.
    Only fires when a facility was matched as genuinely AT this pin's coordinates (see ``_fetch_epa_echo_data``'s exact-match check) - never suggests the name of a merely-nearby facility."""

    def __init__(self) -> None:
        """Initialize with the ``epa_echo`` source slug."""
        super().__init__(source="epa_echo", verbose_name="EPA ECHO")

    def candidates(self, location: Location) -> list[str | None]:
        """Return the exact-site facility's name, when one was matched.

        Returns:
            A single-item list with the facility name, or empty when no exact-site match exists yet (or ever)."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cache_row = LocationCache.get_fresh(location, _CACHE_SOURCE)
        if not cache_row:
            return []
        exact_site = (cache_row.data or {}).get("exact_site")
        if not exact_site:
            return []
        return [exact_site.get("name") or None]


class EpaEchoPlugin(UrbanLensPlugin):
    """EPA ECHO regulated-facility compliance data for pinned locations, via REData. USA only."""

    name: ClassVar[str] = "epa_echo"
    verbose_name: ClassVar[str] = "EPA ECHO"
    description: ClassVar[str] = (
        "EPA Enforcement and Compliance History Online (ECHO) lookup, via REData's shared points-of-interest "
        "endpoint - shows an unconditional compliance detail card when a regulated facility sits at this exact "
        "pin, plus a subscription-gated Nearby Research tab listing nearby facilities and their compliance "
        "status. USA only; strong urbex signal for industrial and contaminated sites."
    )
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - this plugin calls REData's shared
    # points-of-interest lookup (service key "redata_points_of_interest"),
    # already registered by plugins.builtin.yelp.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the exact-site detail card and the nearby-facilities list."""
        return [EpaEchoDetailPanelSource(), EpaEchoNearbyPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the exact-site EPA facility name as an official-name candidate."""
        return [EpaFacilityNameProvider()]
