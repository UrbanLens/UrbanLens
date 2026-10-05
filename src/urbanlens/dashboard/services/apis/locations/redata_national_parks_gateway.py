"""REData-backed gateway for National Park Service park units near a coordinate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import quote

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway
from urbanlens.dashboard.services.core.coalesce import coalesced

_PATH = "/api/v1/parks/nearby/"
_ALERTS_PATH = "/api/v1/parks/{park_code}/alerts/"
_VISITOR_CENTERS_PATH = "/api/v1/parks/{park_code}/visitor-centers/"
_CAMPGROUNDS_PATH = "/api/v1/parks/{park_code}/campgrounds/"
_PLACES_PATH = "/api/v1/parks/{park_code}/places/"
_WEBCAMS_PATH = "/api/v1/parks/{park_code}/webcams/"

#: REData's own default radius for this endpoint (see api-reference.md) -
#: passed explicitly rather than omitted so callers can see the value in one
#: place instead of having to know REData's own default to reason about it.
DEFAULT_RADIUS_METERS = 100_000.0

#: Park units are searched for across tens of kilometres, so every building on a site - and every pin within
#: about a kilometre - gets the same answer; they share one ask per cell of this many decimal degrees.
_SHARED_CELL_DECIMALS = 2
#: The NPS catalog changes on REData's schedule, not per request.
_SHARED_SECONDS = 6 * 60 * 60
#: A unit's alerts (closures, hazards) are refreshed by REData within hours; every pin near the park shares one ask.
_ALERTS_SHARED_SECONDS = 60 * 60


def _as_dict_list(body: Any) -> list[dict[str, Any]]:
    """Coerce a per-park-facet response body into a list of dicts, defensively.

    Args:
        body: The raw decoded JSON body from :meth:`RedataLocationContextGateway.get_json` - expected to already be a plain array for these endpoints.

    Returns:
        ``body`` as a list, dropping any entry that isn't itself a dict; ``[]`` when ``body`` isn't a list at all."""
    if not isinstance(body, list):
        return []
    return [entry for entry in body if isinstance(entry, dict)]


def _park_path(template: str, park_code: str) -> str:
    return template.format(park_code=quote(park_code, safe=""))


@dataclass(slots=True, kw_only=True)
class RedataNationalParksGateway(RedataLocationContextGateway):
    """REST client for REData's local NPS catalog: units near a point, and each unit's facets.

    A unit's facets answer the same for every pin near it, so each is asked once per unit and shared - alerts for an
    hour, the rest for :data:`_SHARED_SECONDS` - rather than once per pin.
    """

    service_key: ClassVar[str] = "redata_national_parks"

    def find_parks_near(self, latitude: float, longitude: float, *, radius_meters: float | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Return NPS park units near a coordinate, nearest first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters, up to REData's 500 km
                ceiling for this endpoint. Omit to use REData's own 100 km
                default.
            limit: Maximum number of units to return (REData default 20, cap
                200).

        Returns:
            ``NationalParkUnitSerializer``-shaped dicts (``park_code``, ``full_name``, ``designation``, ``description``, ``url``, ``states``, ``latitude``, ``longitude``, ``geometry``, ``activities``, ``images``, ``operating_hours``, ...), nearest first - possibly empty.

        Raises:
            LocationContextUnavailableError: The request failed outright or REData reported a transient failure.
        """
        cell_latitude, cell_longitude = round(latitude, _SHARED_CELL_DECIMALS), round(longitude, _SHARED_CELL_DECIMALS)
        return coalesced(
            f"redata:parks-nearby:{cell_latitude},{cell_longitude}:{radius_meters}:{limit}",
            lambda: self.near_point(_PATH, cell_latitude, cell_longitude, radius_meters=radius_meters, limit=limit).results,
            ttl=_SHARED_SECONDS,
        )

    def find_nearest_park(self, latitude: float, longitude: float, *, radius_meters: float | None = None) -> dict[str, Any] | None:
        """Return the single nearest NPS park unit to a coordinate, if any is within range.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters; omit to use REData's own
                100 km default.

        Returns:
            The nearest unit dict (see :meth:`find_parks_near`), or None when no NPS unit falls within the search radius.

        Raises:
            LocationContextUnavailableError: The request failed outright or REData reported a transient failure.
        """
        results = self.find_parks_near(latitude, longitude, radius_meters=radius_meters, limit=1)
        return results[0] if results else None

    def get_alerts(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch published alerts (closures, hazards, cautions) for one NPS park unit.

        Args:
            park_code: The unit's NPS park code (e.g. ``"yell"``) - REData's
                own ``park_code``, already resolved via :meth:`find_nearest_park`
                or :meth:`find_parks_near`. This endpoint is per-park-unit, not
                per-coordinate.

        Returns:
            ``NationalParkAlertSerializer``-shaped dicts (``id``, ``external_id``, ``title``, ``description``, ``category``, ``url``, ``last_indexed_date``, ``fetched_at``) - empty when the park currently has nothing published, which is a normal, cacheable answer, not an error.

        Raises:
            LocationContextUnavailableError: The request failed outright, including a 404 - REData should already know ``park_code`` from the nearby lookup that produced it, so a 404 here means something unexpected happened, not "no alerts".
        """
        path = _park_path(_ALERTS_PATH, park_code)
        return coalesced(f"redata:{path}", lambda: _as_dict_list(self.get_json(path)), ttl=_ALERTS_SHARED_SECONDS)

    def get_visitor_centers(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch visitor centers for one NPS park unit.

        Args:
            park_code: The unit's NPS park code - see :meth:`get_alerts`.

        Returns:
            ``NationalParkVisitorCenterSerializer``-shaped dicts (``id``, ``external_id``, ``name``, ``description``, ``directions_info``, ``url``, ``latitude``, ``longitude``, ``operating_hours``, ``contacts``, ``addresses``, ``fetched_at``) - empty when none are published.

        Raises:
            LocationContextUnavailableError: The request failed outright, including an unexpected 404 - see :meth:`get_alerts`.
        """
        path = _park_path(_VISITOR_CENTERS_PATH, park_code)
        return coalesced(f"redata:{path}", lambda: _as_dict_list(self.get_json(path)), ttl=_SHARED_SECONDS)

    def get_campgrounds(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch campgrounds for one NPS park unit.

        Args:
            park_code: The unit's NPS park code - see :meth:`get_alerts`.

        Returns:
            ``NationalParkCampgroundSerializer``-shaped dicts (``id``, ``external_id``, ``name``, ``description``, ``directions_info``, ``url``, ``latitude``, ``longitude``, ``reservation_info``, ``regulations_overview``, ``amenities``,...

        Raises:
            LocationContextUnavailableError: The request failed outright, including an unexpected 404 - see :meth:`get_alerts`.
        """
        path = _park_path(_CAMPGROUNDS_PATH, park_code)
        return coalesced(f"redata:{path}", lambda: _as_dict_list(self.get_json(path)), ttl=_SHARED_SECONDS)

    def get_places(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch one NPS park unit's points of interest - trailheads, overlooks, ruins, wayside exhibits.

        Args:
            park_code: The unit's NPS park code - see :meth:`get_alerts`.

        Returns:
            ``PointOfInterestSerializer``-shaped dicts (``uuid``, ``provider``, ``external_id``, ``name``, ``category``,
            ``description``, ``url``, ``latitude``, ``longitude``, ``attributes``, ...) - empty when none are published.

        Raises:
            LocationContextUnavailableError: The request failed outright, including REData's 503 when NPS itself is
                rate-limited or down, or an unexpected 404 - see :meth:`get_alerts`.
        """
        path = _park_path(_PLACES_PATH, park_code)
        return coalesced(f"redata:{path}", lambda: _as_dict_list(self.get_json(path)), ttl=_SHARED_SECONDS)

    def get_webcams(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch one NPS park unit's webcams.

        Args:
            park_code: The unit's NPS park code - see :meth:`get_alerts`.

        Returns:
            ``MediaItemSerializer``-shaped dicts of ``kind`` ``webcam`` (``title``, ``url`` - NPS's viewer page -
            ``is_live``, ``embed_url``, ``embed_kind``, ``thumbnail_url``, ...) - empty when none are published.

        Raises:
            LocationContextUnavailableError: As :meth:`get_places`.
        """
        path = _park_path(_WEBCAMS_PATH, park_code)
        return coalesced(f"redata:{path}", lambda: _as_dict_list(self.get_json(path)), ttl=_SHARED_SECONDS)
