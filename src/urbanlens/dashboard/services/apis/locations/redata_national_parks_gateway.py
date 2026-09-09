"""REData-backed gateway for National Park Service park units near a coordinate.

Backs the ``nps`` plugin (``plugins.builtin.nps``) - REData syncs NPS's entire
park-unit catalog wholesale and answers ``GET /api/v1/parks/nearby/`` as a
pure local-catalog read, nearest-first, with no ``providers`` block (see
``../REData/docs/api-reference.md``, "National parks"). This replaces the
direct NPS Developer API + ArcGIS boundary-containment lookup this project
used before (``services.apis.parks.nps``, now removed): REData has no
raw-coordinate containment endpoint of its own (only
``GET /parcels/{uuid}/national-parks/``, keyed by a REData parcel uuid this
project doesn't otherwise resolve for most pins), so the panel's semantics
shift from "the pin sits inside this park's boundary" to "this is the nearest
NPS unit REData's catalog knows about" - see the plugin's own docstring for
the tradeoff.

Also wraps three per-park-unit facets - ``GET /api/v1/parks/{park_code}/alerts/``,
``.../visitor-centers/`` and ``.../campgrounds/`` - each a plain JSON array
rather than the near-a-coordinate envelope, so they go through
:meth:`RedataLocationContextGateway.get_json` instead of :meth:`near_point`.
Unlike the nearby search, these take a ``park_code`` rather than a
coordinate: callers resolve one via :meth:`find_nearest_park` or
:meth:`find_parks_near` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import quote

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_PATH = "/api/v1/parks/nearby/"
_ALERTS_PATH = "/api/v1/parks/{park_code}/alerts/"
_VISITOR_CENTERS_PATH = "/api/v1/parks/{park_code}/visitor-centers/"
_CAMPGROUNDS_PATH = "/api/v1/parks/{park_code}/campgrounds/"

#: REData's own default radius for this endpoint (see api-reference.md) -
#: passed explicitly rather than omitted so callers can see the value in one
#: place instead of having to know REData's own default to reason about it.
DEFAULT_RADIUS_METERS = 100_000.0


def _as_dict_list(body: Any) -> list[dict[str, Any]]:
    """Coerce a per-park-facet response body into a list of dicts, defensively.

    Args:
        body: The raw decoded JSON body from :meth:`RedataLocationContextGateway.get_json`
            - expected to already be a plain array for these endpoints.

    Returns:
        ``body`` as a list, dropping any entry that isn't itself a dict; ``[]``
        when ``body`` isn't a list at all.
    """
    if not isinstance(body, list):
        return []
    return [entry for entry in body if isinstance(entry, dict)]


@dataclass(slots=True, kw_only=True)
class RedataNationalParksGateway(RedataLocationContextGateway):
    """REST client for REData's ``/parks/nearby/`` local NPS catalog lookup."""

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
            ``NationalParkUnitSerializer``-shaped dicts (``park_code``,
            ``full_name``, ``designation``, ``description``, ``url``,
            ``states``, ``latitude``, ``longitude``, ``geometry``,
            ``activities``, ``images``, ``operating_hours``, ...), nearest
            first - possibly empty.

        Raises:
            LocationContextUnavailableError: The request failed outright or
                REData reported a transient failure.
        """
        envelope = self.near_point(_PATH, latitude, longitude, radius_meters=radius_meters, limit=limit)
        return envelope.results

    def find_nearest_park(self, latitude: float, longitude: float, *, radius_meters: float | None = None) -> dict[str, Any] | None:
        """Return the single nearest NPS park unit to a coordinate, if any is within range.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters; omit to use REData's own
                100 km default.

        Returns:
            The nearest unit dict (see :meth:`find_parks_near`), or None when
            no NPS unit falls within the search radius.

        Raises:
            LocationContextUnavailableError: The request failed outright or
                REData reported a transient failure.
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
            ``NationalParkAlertSerializer``-shaped dicts (``id``, ``external_id``,
            ``title``, ``description``, ``category``, ``url``,
            ``last_indexed_date``, ``fetched_at``) - empty when the park
            currently has nothing published, which is a normal, cacheable
            answer, not an error.

        Raises:
            LocationContextUnavailableError: The request failed outright,
                including a 404 - REData should already know ``park_code``
                from the nearby lookup that produced it, so a 404 here means
                something unexpected happened, not "no alerts".
        """
        return _as_dict_list(self.get_json(_ALERTS_PATH.format(park_code=quote(park_code, safe=""))))

    def get_visitor_centers(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch visitor centers for one NPS park unit.

        Args:
            park_code: The unit's NPS park code - see :meth:`get_alerts`.

        Returns:
            ``NationalParkVisitorCenterSerializer``-shaped dicts (``id``,
            ``external_id``, ``name``, ``description``, ``directions_info``,
            ``url``, ``latitude``, ``longitude``, ``operating_hours``,
            ``contacts``, ``addresses``, ``fetched_at``) - empty when none are
            published.

        Raises:
            LocationContextUnavailableError: The request failed outright,
                including an unexpected 404 - see :meth:`get_alerts`.
        """
        return _as_dict_list(self.get_json(_VISITOR_CENTERS_PATH.format(park_code=quote(park_code, safe=""))))

    def get_campgrounds(self, park_code: str) -> list[dict[str, Any]]:
        """Fetch campgrounds for one NPS park unit.

        Args:
            park_code: The unit's NPS park code - see :meth:`get_alerts`.

        Returns:
            ``NationalParkCampgroundSerializer``-shaped dicts (``id``,
            ``external_id``, ``name``, ``description``, ``directions_info``,
            ``url``, ``latitude``, ``longitude``, ``reservation_info``,
            ``regulations_overview``, ``amenities``,
            ``number_of_sites_reservable``,
            ``number_of_sites_first_come_first_serve``, ``contacts``,
            ``addresses``, ``fetched_at``) - empty when none are published.

        Raises:
            LocationContextUnavailableError: The request failed outright,
                including an unexpected 404 - see :meth:`get_alerts`.
        """
        return _as_dict_list(self.get_json(_CAMPGROUNDS_PATH.format(park_code=quote(park_code, safe=""))))
