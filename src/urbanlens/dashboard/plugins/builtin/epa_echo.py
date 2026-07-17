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

import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "epa_echo"

#: A facility whose DFR coordinates are within this distance of the pin's own
#: coordinates is treated as "this facility IS the pin", not just nearby.
_EXACT_MATCH_RADIUS_MILES = 0.1
#: Wall-clock ceiling on the exact-match DFR lookup loop - the real bound on
#: how many of the (up to 10) nearby-search candidates get checked, not a
#: fixed candidate count. Earlier versions of this loop capped the candidate
#: count directly (first 3, then 2, to bound worst-case latency) - but that
#: traded away correctness for no reason once this budget existed: checking
#: fewer candidates directly means missing more genuine exact-site matches
#: whenever the right one isn't checked early - confirmed in production,
#: where reducing the cap to 2 caused a facility that WAS in the nearby
#: results to stop being found. The budget alone already bounds worst-case
#: latency (this whole fetch runs inside a Celery task sharing a worker pool
#: with ~10 other panel fetches on a cold pin page - see docker-compose.yml's
#: celery-worker concurrency comment), so there's no latency reason left to
#: also cap the count. In practice ECHO's OWN 5-calls/minute rate limit (see
#: EpaEchoPlugin.get_service_defaults) is the tighter constraint - it usually
#: allows checking only 2-3 candidates per fetch before RateLimitExceededError
#: cuts the loop short (see _fetch_epa_echo_data) - which is why the loop
#: below checks candidates in latitude-proximity order rather than raw array
#: order, to spend that scarce budget on the ones most likely to be the match.
_EXACT_MATCH_BUDGET_SECONDS = 30.0


def _miles_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from urbanlens.dashboard.models.profile.model import _haversine_km

    return _haversine_km((lat1, lng1), (lat2, lng2)) * 0.621371


def _fetch_epa_echo_data(pin: Pin) -> dict[str, Any]:
    """Search EPA ECHO for nearby facilities and look for an exact-site match among the closest few.

    ECHO's own rate limit (5 calls/minute, see ``EpaEchoPlugin.get_service_defaults``)
    is tight enough that the exact-match loop below - up to 10 ``get_facility_detail``
    calls plus the initial search - routinely exhausts it before finishing. A
    ``RateLimitExceededError`` raised mid-loop is caught and treated as "stop
    checking further candidates", not a fetch failure: the facilities list (and
    whatever exact-site checking completed before the budget ran out) is still
    genuinely useful and must not be thrown away. Letting it propagate would
    abort ``fetch()`` entirely, so nothing gets cached and ``run_panel_fetch``
    suppresses the panel for 30 minutes (``DISABLED_SKIP_TTL_SECONDS``) - which
    is exactly what was happening in production before this fix.

    Every facility this function ever sees (from either the nearby-search or a
    full Detailed Facility Report lookup) is recorded in ``EpaFacility``,
    project-wide - so a candidate already fetched while checking some OTHER
    pin's exact-site match is reused directly from the database instead of
    spending any more of ECHO's rate-limited budget on it. This is what lets
    fetching nearby facilities for any one pin passively build up reusable
    knowledge for the whole area (see EpaFacility's own docstring).

    Returns the shape persisted to the shared LocationCache row:
    ``{"facilities": [...], "exact_site": {...} | None}``.
    """
    from urbanlens.dashboard.models.epa_facility import EpaFacility
    from urbanlens.dashboard.services.apis.locations.epa_echo import EpaEchoGateway
    from urbanlens.dashboard.services.geo_filter import is_usa_coordinates
    from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError

    lat = float(pin.effective_latitude or 0)
    lng = float(pin.effective_longitude or 0)
    if not is_usa_coordinates(lat, lng):
        return {"facilities": [], "exact_site": None}

    gateway = EpaEchoGateway()
    facilities = gateway.get_nearby_facilities(lat, lng, radius_miles=0.5, limit=10)

    for facility in facilities:
        EpaFacility.record_search_result(
            facility.get("registry_id") or "",
            name=facility.get("name") or "",
            address=facility.get("address") or "",
            latitude=facility.get("latitude"),
            data={k: v for k, v in facility.items() if k not in ("registry_id", "latitude")},
        )

    # Candidates whose Detailed Facility Report we've already fetched for some
    # OTHER pin near here don't cost anything to check again - reuse them and
    # spend the rate-limited budget only on genuinely new candidates.
    known_details = EpaFacility.known_details_by_registry_id(facility.get("registry_id") or "" for facility in facilities)

    # ECHO's nearby-search rows aren't distance-sorted (see get_nearby_facilities'
    # docstring) and the rate limit above usually can't survive checking every
    # candidate - so check the ones most likely to actually be the match FIRST.
    # Latitude is the only per-facility coordinate ECHO's search rows include;
    # not a full distance, but a solid cheap proxy given the tight (0.5mi)
    # search radius, and far better than raw array order for finding the real
    # match within whatever budget survives the rate limit. The unsorted
    # `facilities` list (below) is still what gets returned/cached/rendered -
    # this ordering is only used to decide which few candidates spend the
    # scarce DFR-lookup budget.
    facilities_by_proximity = sorted(facilities, key=lambda f: abs(f["latitude"] - lat) if f.get("latitude") is not None else float("inf"))

    exact_site = None
    best_distance = _EXACT_MATCH_RADIUS_MILES
    started = time.monotonic()
    for facility in facilities_by_proximity:
        registry_id = facility.get("registry_id") or ""
        if not registry_id:
            continue

        known = known_details.get(registry_id)
        if known is not None:
            detail: dict[str, Any] | None = {"latitude": known.latitude, "longitude": known.longitude, **known.data}
        else:
            if time.monotonic() - started >= _EXACT_MATCH_BUDGET_SECONDS:
                logger.warning("EPA ECHO exact-site match budget exceeded for pin %s; stopping early", pin.pk)
                break
            try:
                detail = gateway.get_facility_detail(registry_id)
            except RateLimitExceededError:
                logger.warning("EPA ECHO rate limit exhausted mid exact-site match for pin %s; keeping partial results", pin.pk)
                break
            if detail and detail.get("latitude") is not None and detail.get("longitude") is not None:
                EpaFacility.record_detail_result(
                    registry_id,
                    name=facility.get("name") or "",
                    address=facility.get("address") or "",
                    latitude=detail["latitude"],
                    longitude=detail["longitude"],
                    data={k: v for k, v in detail.items() if k not in ("latitude", "longitude")},
                )

        if not detail or detail.get("latitude") is None or detail.get("longitude") is None:
            continue
        distance = _miles_between(lat, lng, detail["latitude"], detail["longitude"])
        if distance <= best_distance:
            best_distance = distance
            exact_site = {**detail, "registry_id": registry_id, "name": facility.get("name") or "", "address": facility.get("address") or ""}

    return {"facilities": facilities, "exact_site": exact_site}


def _propagate_exact_site_to_nearby_locations(location: Location, exact_site: dict[str, Any]) -> None:
    """Apply a newly-confirmed exact-site EPA match to any other pinned Location within
    the exact-match radius whose own ``epa_echo`` cache has no match yet.

    Without this, a Location that happened to strike out on its own exact-site
    check (most often because ECHO's tight rate limit cut its DFR-lookup loop
    short before it reached the right candidate - see ``_fetch_epa_echo_data``'s
    docstring) stays cached with an empty result for up to
    ``SiteSettings.external_data_cache_days``, even after a neighboring pin
    - sometimes fetched moments later - definitively proves the same facility
    sits right there too. Since the facility's own confirmed coordinates are
    already in hand, this costs zero extra EPA API calls: it's a plain
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
    nearby_locations = (
        LocationModel.objects.filter(point__distance_lte=(point, D(mi=_EXACT_MATCH_RADIUS_MILES)))
        .exclude(pk=location.pk)
        .filter(pins__isnull=False)
        .distinct()
    )

    for neighbor in nearby_locations:
        cache_row = LocationCache.objects.filter(location=neighbor, source=_CACHE_SOURCE).first()
        existing_data = cache_row.data if cache_row else {}
        if (existing_data or {}).get("exact_site"):
            continue
        new_data = {**existing_data, "facilities": existing_data.get("facilities") or [], "exact_site": exact_site}
        LocationCache.set(neighbor, _CACHE_SOURCE, new_data, query_key=(cache_row.query_key if cache_row else ""))


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
        data = _fetch_epa_echo_data(pin)
        LocationCache.set(pin.location, self.cache_source, data, query_key=f"{lat:.5f},{lng:.5f}")

        exact_site = data.get("exact_site")
        if exact_site:
            _propagate_exact_site_to_nearby_locations(pin.location, exact_site)

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
            "nested": True,
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
        data = _fetch_epa_echo_data(pin)
        LocationCache.set(pin.location, self.cache_source, data, query_key=f"{lat:.5f},{lng:.5f}")

        exact_site = data.get("exact_site")
        if exact_site:
            _propagate_exact_site_to_nearby_locations(pin.location, exact_site)
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
        from django.core.exceptions import ObjectDoesNotExist

        from urbanlens.dashboard.models.links.model import PinLink, WikiLink

        url = f"https://echo.epa.gov/detailed-facility-report?fid={registry_id}"
        PinLink.objects.get_or_create(pin=pin, url=url, defaults={"name": "EPA Compliance Report"})
        try:
            wiki = location.wiki
        except ObjectDoesNotExist:
            return
        WikiLink.objects.get_or_create(wiki=wiki, url=url, defaults={"name": "EPA Compliance Report"})

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
            # ai_extract: the facility report is a real content page about this
            # exact site, so it offers the AI field-extraction button.
            {"url": f"https://echo.epa.gov/detailed-facility-report?fid={registry_id}", "label": "View full EPA compliance report", "ai_extract": True}
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
