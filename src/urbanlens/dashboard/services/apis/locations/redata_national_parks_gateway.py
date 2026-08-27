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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_PATH = "/api/v1/parks/nearby/"

#: REData's own default radius for this endpoint (see api-reference.md) -
#: passed explicitly rather than omitted so callers can see the value in one
#: place instead of having to know REData's own default to reason about it.
DEFAULT_RADIUS_METERS = 100_000.0


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
