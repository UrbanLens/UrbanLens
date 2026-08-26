"""OpenHistoricalMap gateway: dated vector features via the public OHM Overpass API.

This is a deliberate stopgap ahead of REData's own future temporal-imagery
endpoint (not built yet) - once that ships, this gateway and the plugin/panel/
controller wiring built on it exist to be retired, the same way this project's
direct NASA GIBS/Mapbox/Bing/etc. satellite integrations were already replaced
by ``RedataSatelliteProvider`` (see ``plugins.builtin.satellite_imagery``'s
module docstring). It is a direct integration only because there is nothing
yet on REData's side to swap it for.

https://overpass-api.openhistoricalmap.org/ is OpenHistoricalMap's own fork of
the Overpass API, queryable over OSM's ``start_date``/``end_date`` tagging
scheme extended into OHM's dataset of features that existed (or stopped
existing) at a given time. Free and keyless, but the volunteer-run instance
documents a hard cap of 2 concurrent request slots and a history of being
overwhelmed by careless callers - see ``OpenHistoricalMapPlugin`` for how
conservatively this integration throttles itself as a result.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

logger = logging.getLogger(__name__)

_OVERPASS_URL = "https://overpass-api.openhistoricalmap.org/api/interpreter"

#: Plausible calendar-year bound shared by every OHM query in this module -
#: rejects garbage before it reaches Overpass or a caller. Deliberately loose;
#: OHM's own dataset spans a similarly wide range and this is just a
#: plausibility check, not a real historical-coverage bound (that's what
#: :meth:`OpenHistoricalMapGateway.get_coverage` answers). The single source of
#: truth for both ends of this: :func:`get_coverage` filters its extracted
#: years to this range too, so a stray/garbage-tagged year never lands in a
#: slider's range only to 404 the moment someone picks it.
MIN_YEAR = 1000
MAX_YEAR = 2100

#: Matches the leading 1-4 digit year (optionally negative, for BCE dates) at
#: the start of an OHM ``start_date``/``end_date`` value. Real-world values are
#: messy (e.g. an EDTF-flavored "1849~" living in the plain, non-``:edtf``
#: key) - this only needs the leading year, so it matches and ignores whatever
#: trails it rather than requiring a strict ``YYYY``/``YYYY-MM``/``YYYY-MM-DD``.
_YEAR_RE = re.compile(r"^-?\d{1,4}")


def _extract_year(value: Any) -> int | None:
    """Pull the leading calendar year out of a raw OHM date-tag value.

    Args:
        value: A ``start_date``/``end_date`` tag value, or anything else
            (missing tags surface as ``None`` from ``dict.get``).

    Returns:
        The leading year as an int, or None when ``value`` isn't a string or
        doesn't start with a recognizable year - skipped rather than raised,
        since OHM's real-world tagging is inconsistent enough that a strict
        parse would drop otherwise-usable coverage.
    """
    if not isinstance(value, str):
        return None
    match = _YEAR_RE.match(value.strip())
    return int(match.group()) if match else None


def _way_geometry(element: dict[str, Any]) -> dict[str, Any] | None:
    """Build a LineString/Polygon geometry from a way element's inline ``geometry``.

    Args:
        element: An Overpass ``out geom`` way element.

    Returns:
        A GeoJSON geometry dict, or None when ``geometry`` is missing or
        malformed. A closed way (first point equals last, at least 4 points)
        is treated as a Polygon - a reasonable heuristic for buildings/land
        use, though it will misclassify a genuinely closed-loop line (e.g. a
        roundabout) as a polygon.
    """
    points = element.get("geometry")
    if not isinstance(points, list) or len(points) < 2:
        return None
    try:
        coordinates = [[point["lon"], point["lat"]] for point in points]
    except (KeyError, TypeError):
        return None

    if len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
        return {"type": "Polygon", "coordinates": [coordinates]}
    return {"type": "LineString", "coordinates": coordinates}


def _element_to_feature(element: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one Overpass element into a GeoJSON Feature.

    Args:
        element: One entry from an Overpass ``out geom``/``out tags`` response.

    Returns:
        A GeoJSON Feature dict, or None when the element is a relation
        (multipolygon assembly is out of scope for this pass) or its geometry
        is missing/malformed - skipped rather than raised, so one bad element
        doesn't drop the rest of the response.
    """
    kind = element.get("type")
    tags = element.get("tags")
    properties: dict[str, Any] = dict(tags) if isinstance(tags, dict) else {}
    properties["id"] = f"{kind}/{element.get('id')}"

    if kind == "node":
        lat, lon = element.get("lat"), element.get("lon")
        if lat is None or lon is None:
            return None
        geometry: dict[str, Any] | None = {"type": "Point", "coordinates": [lon, lat]}
    elif kind == "way":
        geometry = _way_geometry(element)
    else:
        # Relations (multipolygons) are skipped - correctly assembling their
        # member ways is out of scope for this pass.
        return None

    if geometry is None:
        return None
    return {"type": "Feature", "geometry": geometry, "properties": properties}


class OpenHistoricalMapUnavailableError(Exception):
    """Raised when an OHM Overpass request fails outright.

    Reserved for network/timeout/malformed-response failure - never for a
    query that reached OHM and simply found nothing nearby, which is a normal
    empty result (``OhmCoverage(available=False, years=[])``, or an empty
    FeatureCollection), not an error.
    """


@dataclass(frozen=True, slots=True)
class OhmCoverage:
    """Whether OHM has any dated coverage near a point, and for which years.

    Attributes:
        available: True when at least one dated (``start_date``/``end_date``
            tagged) feature exists within the query radius.
        years: Every distinct year extracted from those features' start and
            end dates, sorted ascending - a slider tick belongs at both the
            year something appeared and the year it disappeared.
    """

    available: bool
    years: list[int]


@dataclass(slots=True, kw_only=True)
class OpenHistoricalMapGateway(Gateway):
    """Gateway for the free, keyless OpenHistoricalMap Overpass API."""

    service_key: ClassVar[str] = "open_historical_map"
    paid_service: ClassVar[bool] = False

    def get_coverage(self, latitude: float, longitude: float, *, radius_meters: float = 300) -> OhmCoverage:
        """Check whether OHM has any dated features near a point, and for which years.

        Requests tags only (``out tags``, no geometry) - this only needs to
        answer "does dated data exist nearby" and "what years", so there's no
        reason to pay for the geometry payload.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius around the point.

        Returns:
            The coverage summary. ``available=False``/``years=[]`` is a valid,
            normal answer meaning "queried fine, nothing dated found here" -
            distinct from raising, which means the query itself failed.

        Raises:
            OpenHistoricalMapUnavailableError: The request failed outright
                (network/timeout/malformed response).
        """
        query = f'[out:json][timeout:15];\n(\n  nwr(around:{radius_meters},{latitude},{longitude})["start_date"];\n  nwr(around:{radius_meters},{latitude},{longitude})["end_date"];\n);\nout tags;'
        payload = self._query(query)
        elements = payload.get("elements")
        elements = elements if isinstance(elements, list) else []

        years: set[int] = set()
        for element in elements:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags")
            if not isinstance(tags, dict):
                continue
            for key in ("start_date", "end_date"):
                year = _extract_year(tags.get(key))
                if year is not None and MIN_YEAR <= year <= MAX_YEAR:
                    years.add(year)

        return OhmCoverage(available=bool(elements), years=sorted(years))

    def get_features_at(self, latitude: float, longitude: float, year: int, *, radius_meters: float = 300) -> dict[str, Any]:
        """Fetch OHM features that existed at ``year``, as a GeoJSON FeatureCollection.

        Requests full inline geometry (``out geom``) so ways come back without
        a separate node-resolution pass.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            year: The calendar year to query for (a feature "existed" at
                ``year`` when its ``start_date`` is on or before it and, if
                present, its ``end_date`` is on or after it).
            radius_meters: Search radius around the point.

        Returns:
            ``{"type": "FeatureCollection", "features": [...]}``. Relations
            are omitted (see :func:`_element_to_feature`); an element with
            missing/malformed geometry or tags is skipped rather than
            dropping the whole response.

        Raises:
            ValueError: ``year`` is outside the plausible :data:`MIN_YEAR`-
                :data:`MAX_YEAR` range.
            OpenHistoricalMapUnavailableError: The request failed outright
                (network/timeout/malformed response).
        """
        if not MIN_YEAR <= year <= MAX_YEAR:
            raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}, got {year}")

        year_str = str(year)
        query = (
            "[out:json][timeout:20];\n"
            "(\n"
            f'  nwr(around:{radius_meters},{latitude},{longitude})["start_date"]'
            f'(if: t["start_date"] <= "{year_str}" && (!is_tag("end_date") || t["end_date"] >= "{year_str}"));\n'
            f'  nwr(around:{radius_meters},{latitude},{longitude})["end_date"]'
            f'(if: (!is_tag("start_date") || t["start_date"] <= "{year_str}") && t["end_date"] >= "{year_str}");\n'
            ");\n"
            "out geom;"
        )
        payload = self._query(query)
        elements = payload.get("elements")
        elements = elements if isinstance(elements, list) else []

        features: list[dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            feature = _element_to_feature(element)
            if feature is not None:
                features.append(feature)

        return {"type": "FeatureCollection", "features": features}

    def _query(self, query: str) -> dict[str, Any]:
        """POST one Overpass QL query and return its decoded JSON body.

        Args:
            query: A complete Overpass QL query string.

        Returns:
            The decoded JSON response body.

        Raises:
            OpenHistoricalMapUnavailableError: Connection/timeout failure, a
                non-200 response, an unparseable/malformed body, or this
                service's own rate limit/disablement being hit
                (``RequestCancelledError`` and its ``RateLimitExceededError``/
                ``ServiceDisabledError`` subclasses - a real, expected outcome
                here since OHM's global budget is shared across every viewer's
                on-demand requests plus the background coverage panel, not
                just a defensive catch-all).
        """
        try:
            response = self.session.post(_OVERPASS_URL, data={"data": query}, timeout=20)
        except OSError as exc:
            raise OpenHistoricalMapUnavailableError(f"Could not reach the OpenHistoricalMap Overpass API: {exc}") from exc
        except RequestCancelledError as exc:
            raise OpenHistoricalMapUnavailableError(f"OpenHistoricalMap Overpass API request was rate-limited or disabled: {exc}") from exc

        if response.status_code != 200:
            raise OpenHistoricalMapUnavailableError(f"OpenHistoricalMap Overpass API returned HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenHistoricalMapUnavailableError("OpenHistoricalMap Overpass API returned an unparseable response.") from exc

        if not isinstance(payload, dict):
            raise OpenHistoricalMapUnavailableError("OpenHistoricalMap Overpass API returned an unexpected response shape.")
        return payload
