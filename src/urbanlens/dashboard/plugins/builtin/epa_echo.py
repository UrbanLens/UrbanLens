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

Backed by REData's shared points-of-interest lookup (``provider="epa_echo"``)
rather than the direct EPA ECHO REST API this project used before
(``services.apis.locations.epa_echo``, now removed). That direct API needed a
two-step, tightly rate-limited (5 calls/minute) dance - a nearby-search call
with no per-facility longitude, then a separate Detailed Facility Report call
per candidate to get real coordinates and compliance detail - which is why the
old version of this module had a wall-clock exact-match budget and a
closest-by-latitude candidate ordering to spend that scarce per-candidate
budget wisely. REData's own ``epa_echo`` provider resolves every candidate's
coordinates and compliance attributes in the single lookup call, so none of
that per-candidate budgeting exists anymore - see :func:`_fetch_epa_echo_data`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
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
        Distance in miles.
    """
    from urbanlens.dashboard.models.profile.meta import DistanceUnit
    from urbanlens.dashboard.services.core.units import km_to_display
    from urbanlens.dashboard.services.geo.distance import haversine_km

    return km_to_display(haversine_km(lat1, lng1, lat2, lng2), DistanceUnit.MILES)


def _facility_from_poi(poi: dict[str, Any]) -> dict[str, Any]:
    """Map a REData ``epa_echo`` points-of-interest row onto this plugin's facility shape.

    REData's own docs (``../REData/docs/api-reference.md``, "Points of
    interest") describe ``epa_echo`` rows as carrying "compliance status,
    quarters in non-compliance, significant-violator flag, last inspection" in
    ``attributes`` - assumed here as ``compliance_status``,
    ``quarters_in_noncompliance``, ``significant_violator`` and
    ``last_inspection`` (snake_case, matching REData's own convention
    elsewhere), pending REData's ``epa_echo`` provider module actually landing
    to confirm exact key spelling. ``address`` is likewise read from
    ``attributes`` since the generic ``PointOfInterest`` model has no address
    column of its own.

    Unlike the direct EPA ECHO API this replaced - whose Detailed Facility
    Report broke compliance history down per environmental statute (RCRA, CAA,
    CWA, ...) - REData's documented attributes are a single flattened status
    per facility, not a per-program list. A facility's history across
    multiple statutes is no longer distinguishable; only its overall status is.

    Args:
        poi: One ``PointOfInterestSerializer``-shaped row with ``provider="epa_echo"``.

    Returns:
        ``{"registry_id", "name", "address", "latitude", "longitude",
        "compliance_status", "significant_violator",
        "quarters_in_noncompliance", "last_inspection"}``.
    """
    attributes = poi.get("attributes") or {}
    return {
        "registry_id": poi.get("external_id") or "",
        "name": poi.get("name") or "",
        "address": attributes.get("address") or "",
        "latitude": poi.get("latitude"),
        "longitude": poi.get("longitude"),
        "compliance_status": attributes.get("compliance_status") or "",
        "significant_violator": bool(attributes.get("significant_violator")),
        "quarters_in_noncompliance": attributes.get("quarters_in_noncompliance"),
        "last_inspection": attributes.get("last_inspection") or "",
    }


def _fetch_epa_echo_data(pin: Pin) -> dict[str, Any]:
    """Search REData for nearby EPA-regulated facilities and pick out an exact-site match.

    REData's own ``epa_echo`` points-of-interest provider resolves every
    candidate's coordinates and compliance attributes in one call - unlike the
    direct EPA ECHO API this replaced, there is no separate, rate-limited
    per-candidate detail fetch left to budget (see the module docstring).

    Every facility this function sees is still recorded in ``EpaFacility``,
    project-wide, exactly as before - reusable by any other pin's own
    exact-site check, and by :class:`EpaFacilityNameProvider`, without a
    second REData call.

    Returns the shape persisted to the shared LocationCache row:
    ``{"facilities": [...], "exact_site": {...} | None}``.
    """
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

    Both panel sources' ``fetch()`` methods are this exact sequence (they
    deliberately share one ``LocationCache`` row - see the module docstring),
    so it lives here once instead of being duplicated in each.

    Args:
        pin: The pin whose location's EPA data should be (re)fetched.

    Returns:
        The freshly-cached payload, so a caller can act on ``exact_site``
        without re-reading the cache row.
    """
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
    """Apply a newly-confirmed exact-site EPA match to any other pinned Location within
    the exact-match radius whose own ``epa_echo`` cache has no match yet.

    Without this, a Location that happened to strike out on its own exact-site
    check stays cached with an empty result for up to
    ``SiteSettings.external_data_cache_days``, even after a neighboring pin
    - sometimes fetched moments later - definitively proves the same facility
    sits right there too. Since the facility's own confirmed coordinates are
    already in hand, this costs zero extra REData calls: it's a plain
    proximity query against already-pinned Locations, writing the same
    ``exact_site`` payload directly into their cache rows.

    Never overwrites a Location that already has its own confirmed
    ``exact_site`` - only fills in rows that are missing or empty, so a
    genuinely different real match is never clobbered.

    Args:
        location: The Location the match was just confirmed for (excluded
            from the neighbor search - it already has the match).
        exact_site: The confirmed exact-site payload, including its own
            ``latitude``/``longitude``.
    """
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
    # The subscription gate as a fact about the source rather than only as an
    # entry in a controller's tab dict: any surface that serves this panel -
    # the web tab strip, the external API, whatever comes next - can now check
    # the same field instead of each keeping its own list and eventually
    # disagreeing about which panels are gated. Its sibling
    # EpaEchoDetailPanelSource deliberately has no required_feature: an
    # exact-site compliance card is the integration's primary purpose and is
    # shown to everyone. See PinController._NEARBY_RESEARCH_TABS.
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
        data = _fetch_and_cache(pin)

        exact_site = data.get("exact_site")
        if exact_site:
            registry_id = exact_site.get("registry_id")
            if registry_id:
                self._add_echo_report_link(pin, pin.location, registry_id)

    @staticmethod
    def _add_echo_report_link(pin: Pin, location: Location, registry_id: str) -> None:
        """Add the EPA ECHO compliance report URL to the pin's (and wiki's) links, if not already there.

        Mirrors NominatimPanelSource._add_osm_link's pattern for auto-adding a
        confirmed-relevant external report link once a facility is matched to
        this exact pin - see render_context's footer_link for the same URL
        shown inline on the detail card itself.

        Args:
            pin: The pin whose links should include this URL.
            location: The pin's location, for reaching its wiki (if any).
            registry_id: The EPA FRS Registry ID of the matched facility.
        """
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
