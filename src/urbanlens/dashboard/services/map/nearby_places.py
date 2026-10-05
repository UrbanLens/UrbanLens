"""The Places layer: landmarks, national parks and Wikipedia articles near a map click.

A click is snapped to a grid cell and each source is queried at the cell's centre, so a cached answer
describes exactly the key it is stored under and neighbouring clicks share it. Each source is cached on
its own: one source failing leaves the others' answers usable and cached, and the failure is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.apis.request_upstreams import NationalParksUpstream, NearbyLandmarksUpstream, WikipediaNearbyUpstream
from urbanlens.dashboard.services.core.request_upstream import Outcome, wait_all

if TYPE_CHECKING:
    from urbanlens.dashboard.services.core.request_upstream import Pending

logger = logging.getLogger(__name__)

#: Degrees per grid cell (~2 km).
GRID_DEGREES = 0.02

#: The radii landmark search is asked for, so a client choosing any integer cannot make every request a miss.
RADIUS_BUCKETS_M: tuple[int, ...] = (500, 1000, 2000, 5000)

#: Landmarks are only meaningful when zoomed in far enough for the radius to matter.
LANDMARKS_MIN_ZOOM = 10

#: One budget for all three sources together.
NEARBY_DEADLINE = 8.0

_WIKIPEDIA_RADIUS_M = 5000
_WIKIPEDIA_LIMIT = 15
_PARKS_LIMIT = 20

_US_STATE_CODES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "Washington, D.C.",
}


def radius_bucket(requested: int) -> int:
    """The smallest bucket covering *requested*, or the largest bucket.

    Args:
        requested: The radius the client asked for, in metres.

    Returns:
        One of :data:`RADIUS_BUCKETS_M`.
    """
    return next((bucket for bucket in RADIUS_BUCKETS_M if bucket >= requested), RADIUS_BUCKETS_M[-1])


def grid_cell(latitude: float, longitude: float) -> tuple[float, float]:
    """The centre of the grid cell a coordinate falls in.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The cell centre, rounded so equal cells compare equal.
    """
    return round(round(latitude / GRID_DEGREES) * GRID_DEGREES, 4), round(round(longitude / GRID_DEGREES) * GRID_DEGREES, 4)


def expand_state_codes(states: str) -> str:
    """Expand comma-separated US state abbreviations to full state names.

    Args:
        states: e.g. ``"NY,NJ"``.

    Returns:
        e.g. ``"New York, New Jersey"``; unknown codes are kept.
    """
    codes = [code.strip().upper() for code in (states or "").split(",") if code.strip()]
    return ", ".join(_US_STATE_CODES.get(code, code) for code in codes)


@dataclass(frozen=True, slots=True)
class NearbySources:
    """Which sources a caller wants.

    Attributes:
        landmarks: Historical landmarks from REData or Google.
        parks: National parks.
        wikipedia: Geotagged Wikipedia articles.
    """

    landmarks: bool
    parks: bool
    wikipedia: bool


@dataclass(slots=True)
class NearbyPlaces:
    """The combined answer.

    Attributes:
        places: Place dicts in the Places layer's marker shape.
        cached: Whether every source answered from the cache.
        incomplete: Sources that did not answer this time.
    """

    places: list[dict[str, Any]] = field(default_factory=list)
    cached: bool = True
    incomplete: list[str] = field(default_factory=list)


def _landmarks(latitude: float, longitude: float, radius: int, api_key: str) -> list[dict[str, Any]]:
    from urbanlens.dashboard.services.apis.locations import places_resolution

    places: list[dict[str, Any]] = []
    for result in places_resolution.search_nearby_landmarks(latitude, longitude, radius, ["historical_landmark"], api_key=api_key):
        location = result.get("location") or {}
        place_lat, place_lng = location.get("latitude"), location.get("longitude")
        if place_lat is None or place_lng is None:
            continue
        display_name = result.get("displayName") or {}
        places.append(
            {
                "place_id": result.get("id", ""),
                "name": display_name.get("text", "") if isinstance(display_name, dict) else str(display_name),
                "lat": place_lat,
                "lng": place_lng,
                "source": "google",
                "rating": result.get("rating"),
                "user_ratings_total": result.get("userRatingCount"),
                "vicinity": result.get("shortFormattedAddress", ""),
                "types": result.get("types", []),
                "icon": "",
                "description": "",
                "url": "",
            }
        )
    return places


def _parks(latitude: float, longitude: float) -> list[dict[str, Any]]:
    from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

    return [
        {
            "place_id": f"nps_{park.get('park_code', '')}",
            "name": park.get("full_name", ""),
            "lat": park.get("latitude"),
            "lng": park.get("longitude"),
            "source": "nps",
            "description": park.get("description", ""),
            "url": park.get("url", ""),
            "types": ["national_park"],
            "rating": None,
            "vicinity": expand_state_codes(park.get("states", "")),
            "icon": "",
        }
        for park in RedataNationalParksGateway().find_parks_near(latitude, longitude, limit=_PARKS_LIMIT)
    ]


def _wikipedia(latitude: float, longitude: float) -> list[dict[str, Any]]:
    from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

    return WikipediaGateway().get_nearby_articles(latitude, longitude, radius_m=_WIKIPEDIA_RADIUS_M, limit=_WIKIPEDIA_LIMIT)


def find_nearby_places(latitude: float, longitude: float, *, radius: int, sources: NearbySources, caller: str | None, ttl: int) -> NearbyPlaces:
    """Places near a coordinate from every wanted source, fetched in parallel under one deadline.

    Args:
        latitude: WGS-84 latitude of the click.
        longitude: WGS-84 longitude of the click.
        radius: Landmark search radius the client asked for, in metres; snapped to a bucket.
        sources: Which sources to ask.
        caller: Who to charge against each source's per-account rate.
        ttl: Seconds to cache each source's answer.

    Returns:
        The combined places, and which sources did not answer.
    """
    from urbanlens.UrbanLens.settings.app import settings

    cell_lat, cell_lng = grid_cell(latitude, longitude)
    cell = f"{cell_lat:.4f}:{cell_lng:.4f}"
    redata_configured = bool(settings.redata_api_url and settings.redata_api_key)
    api_key = settings.google_unrestricted_api_key or ""

    pending: list[tuple[str, Pending[list[dict[str, Any]]]]] = []
    if sources.landmarks and (api_key or redata_configured):
        from urbanlens.dashboard.services.apis.locations import places_resolution

        bucket = radius_bucket(radius)
        key = f"{places_resolution.active_provider()}:{cell}:{bucket}"
        pending.append(("landmarks", NearbyLandmarksUpstream.start(lambda: _landmarks(cell_lat, cell_lng, bucket, api_key), key=key, ttl=ttl, caller=caller)))
    if sources.parks and redata_configured:
        pending.append(("parks", NationalParksUpstream.start(lambda: _parks(cell_lat, cell_lng), key=cell, ttl=ttl, caller=caller)))
    if sources.wikipedia:
        pending.append(("wikipedia", WikipediaNearbyUpstream.start(lambda: _wikipedia(cell_lat, cell_lng), key=cell, ttl=ttl, caller=caller)))

    wait_all([call for _, call in pending], timeout=NEARBY_DEADLINE)

    answer = NearbyPlaces()
    for source, call in pending:
        result = call.result(0)
        if result.outcome is not Outcome.CACHED:
            answer.cached = False
        if result.ok:
            answer.places.extend(result.value_or([]))
        elif result.outcome is not Outcome.REFUSED:
            # A refused point was never asked and cannot be answered later either, so the source is not missing.
            answer.incomplete.append(source)
    return answer
