"""Overpass API client for deriving OpenStreetMap feature data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
import logging
import random
import re
import time
from typing import Any, ClassVar, Literal, Protocol

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point, Polygon
from django.core.cache import cache
from django.utils import timezone
import requests

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, _is_reasonable_default, best_polygon_from_geometry
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError

logger = logging.getLogger(__name__)

_API_URL = "https://overpass-api.de/api/interpreter"
# Load is spread across this whole pool (see `OverpassGateway.query`): every
# instance runs the same OSM3S/Overpass API software, so an identical query works
# against any of them. The canonical overpass-api.de is chronically overloaded;
# the community mirrors below routinely answer the same query in well under a
# second, so treating them as equal peers rather than fallbacks is deliberate.
# See https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances
_API_MIRRORS: tuple[str, ...] = (
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
# HTTP statuses that mean "this instance is overloaded/unhealthy right now"
# rather than "your query is wrong". 429 is the per-IP slot/quota limit;
# 502/503/504 are the dispatcher-busy family. These, plus network timeouts,
# take an instance out of rotation until the next day (see `_mark_endpoint_down`).
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_RETRY_BACKOFF_SECONDS = 0.5
# Cache key namespace for the "this endpoint is down" flags. Backed by the
# shared Django cache (Valkey/Redis in deployed environments) so a down mark set
# by one worker keeps every other worker off that instance too.
_DOWN_CACHE_KEY = "overpass:endpoint_down:{}"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"


def _seconds_until_next_day() -> int:
    """Seconds from now until the next UTC midnight.

    Used as the TTL for a downed-endpoint flag so a failed instance is retried
    at the start of the next day. The project runs in UTC (``TIME_ZONE``), so
    "the next day" is the next UTC calendar day.
    """
    now = timezone.now()
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_midnight - now).total_seconds()))


def _mark_endpoint_down(url: str) -> None:
    """Take an Overpass endpoint out of rotation until the next day.

    Cache failures are swallowed: if the shared cache is unavailable we simply
    can't remember the outage, which is safe (the endpoint just gets tried
    again) and must never break a boundary lookup.
    """
    try:
        cache.set(_DOWN_CACHE_KEY.format(url), 1, timeout=_seconds_until_next_day())
    except Exception:
        logger.debug("Could not record Overpass endpoint %s as down", url, exc_info=True)


def _endpoint_is_down(url: str) -> bool:
    """Whether an endpoint is currently flagged down. Fails open on cache errors."""
    try:
        return cache.get(_DOWN_CACHE_KEY.format(url)) is not None
    except Exception:
        return False
# Overpass QL has no OR operator to chain bracket filters within one statement, so
# each top-level `|`-separated clause here becomes its own unioned statement per
# element type (see `_TAG_FILTER_CLAUSE_SPLIT` / `_nearby_features_query`). The
# split only breaks on a `|` between a `]` and a `[`, so the `|` inside the regex
# alternation below is left intact.
_DEFAULT_FEATURE_TAG_FILTER = '[~"^(building|amenity|tourism|historic|leisure|landuse|industrial|man_made|shop|office)$"~"."]|["railway"="station"]'
_TAG_FILTER_CLAUSE_SPLIT = re.compile(r"(?<=\])\|(?=\[)")
OsmElementType = Literal["node", "way", "relation"]


def _polygon_from_ring(coords: list[tuple[float, float]]) -> Polygon | None:
    if len(coords) < 4:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    try:
        geom = GEOSGeometry(json.dumps({"type": "Polygon", "coordinates": [coords]}), srid=4326)
    except (TypeError, ValueError):
        return None
    if geom.empty or not isinstance(geom, Polygon):
        return None
    if not geom.valid:
        geom = geom.buffer(0)
    return best_polygon_from_geometry(geom)


def _coords_from_geometry(geometry: list) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for node in geometry:
        try:
            coords.append((float(node["lon"]), float(node["lat"])))
        except (KeyError, TypeError, ValueError):
            return []
    return coords


def _outer_rings_from_element(element: dict) -> list[list[tuple[float, float]]]:
    if isinstance(element.get("geometry"), list):
        return [_coords_from_geometry(element["geometry"])]

    members = element.get("members")
    if not isinstance(members, list):
        return []

    rings: list[list[tuple[float, float]]] = []
    for member in members:
        if member.get("role") not in {"outer", ""} or not isinstance(member.get("geometry"), list):
            continue
        coords = _coords_from_geometry(member["geometry"])
        if coords:
            rings.append(coords)
    return rings


def _polygon_from_element(element: dict) -> Polygon | None:
    """Extract the best polygon from an Overpass way or multipolygon relation."""
    rings = _outer_rings_from_element(element)
    raw_polygons = [_polygon_from_ring(ring) for ring in rings]
    polygons: list[Polygon] = [polygon for polygon in raw_polygons if polygon is not None]
    if not polygons:
        return None
    return max(polygons, key=lambda polygon: polygon.area)


@dataclass(slots=True, kw_only=True)
class OverpassGateway(Gateway, BoundaryProvider):
    """Fetch OpenStreetMap elements from Overpass."""

    service_key: ClassVar[str] = "overpass"
    paid_service: ClassVar[bool] = False

    base_url: str = _API_URL
    mirrors: tuple[str, ...] = _API_MIRRORS
    #: Server-side Overpass QL ``[timeout:N]``. Overpass charges the time spent
    #: waiting for a free dispatcher slot against this budget, so a low value
    #: makes the busy public instances 504 ("Dispatcher_Client ... timeout")
    #: before the (usually sub-second) query even starts. Keep it generous; the
    #: HTTP ``timeout`` below must stay strictly larger so the socket does not
    #: abort a query the server is still willing to run.
    ql_timeout: int = 25
    timeout: int = 30
    radius_meters: int = 100

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def _endpoints(self) -> list[str]:
        """Ordered, de-duplicated list of every configured Overpass endpoint."""
        endpoints: list[str] = []
        for url in (self.base_url, *self.mirrors):
            if url not in endpoints:
                endpoints.append(url)
        return endpoints

    def _available_endpoints(self) -> list[str]:
        """Configured endpoints not currently flagged down, in randomised order.

        Shuffling spreads load evenly across the healthy pool: each query starts
        at a different instance rather than always hammering the primary, and any
        endpoint that then fails is the next-in-line to be retried.
        """
        available = [url for url in self._endpoints() if not _endpoint_is_down(url)]
        random.shuffle(available)
        return available

    def query(self, query: str, *, timeout: int | None = None) -> dict[str, Any]:
        """Run a raw Overpass QL query and return the decoded JSON payload.

        Load is distributed across the pool of public Overpass instances: a
        random healthy endpoint is tried first, failing over to the next on the
        transient overload responses (429/502/503/504) and connection timeouts
        the free instances routinely return. Any endpoint that fails that way is
        taken out of rotation until the next day (:func:`_mark_endpoint_down`),
        so a chronically overloaded instance stops being tried at all.

        A non-retryable HTTP error (e.g. 400 for a malformed query) is raised
        immediately without downing the endpoint - that is our bug, not the
        instance's, and every mirror would reject it identically. Our own
        :class:`RateLimitExceededError` likewise propagates untouched.

        Args:
            query: The Overpass QL program to execute.
            timeout: Optional HTTP timeout override in seconds; defaults to
                ``self.timeout``.

        Returns:
            The decoded JSON payload, or an empty dict if the response was not a
            JSON object or every endpoint is currently down.

        Raises:
            requests.RequestException: If every available endpoint fails
                transiently, or on the first non-retryable HTTP error.
            RateLimitExceededError: If our own rate limiter blocks the call.
        """
        http_timeout = timeout or self.timeout
        candidates = self._available_endpoints()
        if not candidates:
            logger.warning("All Overpass endpoints are flagged down until the next day; skipping query")
            return {}
        last_error: requests.RequestException | None = None
        for attempt, url in enumerate(candidates):
            if attempt:
                time.sleep(_RETRY_BACKOFF_SECONDS)
            try:
                response = self.session.post(url, data={"data": query}, timeout=http_timeout)
            except RateLimitExceededError:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                _mark_endpoint_down(url)
                logger.debug("Overpass endpoint %s unreachable; marked down, trying next", url, exc_info=True)
                continue
            if response.status_code in _RETRYABLE_STATUS:
                last_error = requests.HTTPError(f"{response.status_code} Server Error from {url}", response=response)
                _mark_endpoint_down(url)
                logger.debug("Overpass endpoint %s returned %d; marked down, trying next", url, response.status_code)
                continue
            # Any remaining non-2xx (e.g. 400) is a query-level error identical
            # across every mirror - surface it without downing this endpoint.
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        if last_error is not None:
            raise last_error
        return {}

    def elements_for_query(self, query: str, *, timeout: int | None = None) -> list[dict[str, Any]]:
        """Run Overpass QL and return the element list, logging failures as empty results."""
        try:
            payload = self.query(query, timeout=timeout)
        except (requests.RequestException, ValueError):
            # Reached only after `query` has already failed over across every
            # configured mirror (see `OverpassGateway.query`), so this is a genuine
            # all-instances-unavailable event, not the routine single-instance 504
            # it used to swallow. Still non-fatal - callers treat an empty result
            # as "no boundary data" - but a sustained run of these now warrants a
            # look. logger.warning (not .exception) so it doesn't read as a crash,
            # matching GDELT's gateway, which handles its external failures the same.
            logger.warning("Overpass query failed on all endpoints", exc_info=True)
            return []
        elements = payload.get("elements")
        return elements if isinstance(elements, list) else []

    def nearby_features(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_meters: int = 100,
        tag_filter: str = _DEFAULT_FEATURE_TAG_FILTER,
        include_nodes: bool = True,
        include_geometry: bool = False,
    ) -> list[dict[str, Any]]:
        """Return OSM features around a coordinate for generic location enrichment."""
        query = self._nearby_features_query(
            latitude,
            longitude,
            radius_meters=radius_meters,
            tag_filter=tag_filter,
            include_nodes=include_nodes,
            include_geometry=include_geometry,
            ql_timeout=self.ql_timeout,
        )
        return self.elements_for_query(query)

    def nearby_boundary_candidates(self, latitude: float, longitude: float, radius_meters: int = 100) -> list[dict[str, Any]]:
        """Return OSM ways/relations likely to describe a real place boundary near a coordinate."""
        return self.nearby_features(latitude, longitude, radius_meters=radius_meters, include_nodes=False, include_geometry=True)

    def element(self, element_type: OsmElementType, osm_id: int, *, include_geometry: bool = True) -> dict[str, Any] | None:
        """Return a single OSM node, way, or relation by id via Overpass."""
        out_clause = "out tags geom;" if include_geometry else "out tags center;"
        query = f"""
[out:json][timeout:{self.ql_timeout}];
{element_type}({int(osm_id)});
{out_clause}
""".strip()
        elements = self.elements_for_query(query)
        return elements[0] if elements else None

    @staticmethod
    def _nearby_features_query(
        latitude: float,
        longitude: float,
        *,
        radius_meters: int,
        tag_filter: str,
        include_nodes: bool,
        include_geometry: bool,
        ql_timeout: int = 25,
    ) -> str:
        """Build an Overpass QL query constrained to useful place tags.

        ``tag_filter`` may contain multiple ``|``-separated Overpass filter clauses;
        each becomes its own unioned statement per element type, since Overpass QL
        has no OR operator for chaining bracket filters within a single statement.

        ``ql_timeout`` sets the server-side ``[timeout:N]``; see the
        :class:`OverpassGateway.ql_timeout` field for why it must be generous.
        """
        radius = max(10, min(int(radius_meters), 250))
        lat = float(latitude)
        lon = float(longitude)
        clauses = _TAG_FILTER_CLAUSE_SPLIT.split(tag_filter)
        selectors = []
        for clause in clauses:
            if include_nodes:
                selectors.append(f"  node(around:{radius},{lat:.7f},{lon:.7f}){clause};")
            selectors.extend(
                [
                    f"  way(around:{radius},{lat:.7f},{lon:.7f}){clause};",
                    f'  relation(around:{radius},{lat:.7f},{lon:.7f})["type"="multipolygon"]{clause};',
                ],
            )
        out_clause = "out tags geom qt;" if include_geometry else "out center tags qt;"
        return f"""
[out:json][timeout:{ql_timeout}];
(
{chr(10).join(selectors)}
);
{out_clause}
""".strip()

    @staticmethod
    def _is_building_element(element: dict) -> bool:
        """True when an OSM element's tags describe a building footprint.

        Anything else (landuse, amenity perimeter, leisure grounds, industrial
        sites...) is treated as a property boundary - ambiguity resolves to
        property.
        """
        tags = element.get("tags")
        if not isinstance(tags, dict):
            return False
        return bool(tags.get("building") or tags.get("building:part"))

    def _containing_polygons_by_kind(self, latitude: float, longitude: float) -> dict[str, list[Polygon]]:
        """Collect polygons containing the point, split into building/property kinds."""
        point = Point(float(longitude), float(latitude), srid=4326)
        candidates: dict[str, list[Polygon]] = {"building": [], "property": []}
        for element in self.nearby_boundary_candidates(latitude, longitude, self.radius_meters):
            polygon = _polygon_from_element(element)
            if polygon is None or not _is_reasonable_default(polygon):
                continue
            if polygon.contains(point) or polygon.touches(point):
                kind = "building" if self._is_building_element(element) else "property"
                candidates[kind].append(polygon)
        return candidates

    def get_typed_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> dict[str, Polygon | None]:
        """Return the smallest containing building footprint and property perimeter.

        One Overpass query yields both kinds: elements tagged ``building`` (or
        ``building:part``) become the building boundary; every other matching
        feature (landuse, amenity, leisure, industrial...) competes for the
        property boundary.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Unused; Overpass matches spatially.

        Returns:
            Mapping with "building" and "property" keys (values may be None).
        """
        candidates = self._containing_polygons_by_kind(latitude, longitude)
        return {kind: (min(polygons, key=lambda polygon: polygon.area) if polygons else None) for kind, polygons in candidates.items()}

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """Smallest containing polygon of any kind (property preferred, else building)."""
        typed = self.get_typed_boundaries(latitude, longitude, name=name)
        return typed.get("property") or typed.get("building")
