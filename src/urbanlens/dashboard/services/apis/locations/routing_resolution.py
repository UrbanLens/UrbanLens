"""Resolves route-between-waypoints calls to REData or direct OSRM, per call.

Single chokepoint for the "which provider answers a routing call" decision,
mirroring ``cid_resolution.py``/``places_resolution.py``/``weather_resolution.py``'s
precedent for the same REData-vs-direct choice:

- REData configured (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` both set) -
  the primary deployment's path. ``POST /routes/`` with ``capability:
  "as_given"`` (visit waypoints in the given order - the only capability
  UrbanLens itself ever needs, see ``services.trips.trip_legs``).
- REData not configured, or its request fails - falls back to the existing
  direct ``OSRMGateway`` (free, keyless, self-hostable), preserving today's
  behavior exactly.

Both paths return the same ``{"distance_meters", "duration_seconds"}`` shape
(or None), so ``trip_legs.compute_legs`` never branches on which provider
answered.
"""

from __future__ import annotations

import logging
from typing import Any

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.apis.locations.redata_routing_gateway import RedataRoutingGateway

logger = logging.getLogger(__name__)


def get_route_between(origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
    """Route between two points, trying REData then direct OSRM.

    Args:
        origin: ``(latitude, longitude)`` of the starting point.
        destination: ``(latitude, longitude)`` of the destination.

    Returns:
        ``{"distance_meters", "duration_seconds"}``, or None when no route
        connects the points (or every provider failed).
    """
    if redata_configured():
        try:
            return RedataRoutingGateway().get_route([origin, destination], capability="as_given", profile="driving")
        except LocationContextUnavailableError as exc:
            logger.warning("REData routing failed, falling back to direct OSRM: %s", exc)

    from urbanlens.dashboard.services.apis.routing.osrm import OSRMGateway

    return OSRMGateway().get_route_between(origin, destination)
