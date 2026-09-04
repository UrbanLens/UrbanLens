"""Gateway for REData's ``POST /routes/`` - route between waypoints.

See ``../REData/docs/api-reference.md``, "POST /routes/ - route between
waypoints". Not cacheable (a route is a function of its waypoint list, an
unbounded space), so unlike every other gateway in this package this is a
plain passthrough with no REData-side cache to lean on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

#: Matches REData's own ``profile`` values.
RoutingProfile = Literal["driving", "walking", "cycling"]
#: ``"as_given"`` visits waypoints in the supplied order (OSRM, keyless);
#: ``"optimized"`` reorders them to minimise travel (RouteXL, needs REData's
#: own ``RD_ROUTEXL_USERNAME``/``RD_ROUTEXL_PASSWORD``) - REData returns
#: ``503`` rather than silently downgrading when the requested capability
#: isn't configured on its end.
RoutingCapability = Literal["as_given", "optimized"]


@dataclass(slots=True, kw_only=True)
class RedataRoutingGateway(RedataLocationContextGateway):
    """REST client for REData's routing endpoint."""

    service_key: ClassVar[str] = "redata_routing"

    def get_route(self, waypoints: list[tuple[float, float]], *, capability: RoutingCapability = "as_given", profile: RoutingProfile = "driving") -> dict[str, Any] | None:
        """Route between an ordered list of waypoints.

        Args:
            waypoints: Ordered ``(latitude, longitude)`` pairs, 2-20 per
                REData's own limit.
            capability: ``"as_given"`` or ``"optimized"`` - see the module
                docstring; never substituted for one another server-side.
            profile: ``"driving"``, ``"walking"``, or ``"cycling"``.

        Returns:
            ``{"distance_meters", "duration_seconds"}`` for the whole route
            (matching ``OSRMGateway.get_route``'s shape, its direct-fallback
            counterpart), or None when REData confirmed no route connects
            these points (``route: null``), or found nothing to report.

        Raises:
            LocationContextUnavailableError: The requested ``capability``
                isn't configured on REData's end, or the request itself
                failed outright.

        Note:
            REData's own ``../REData/docs/api-reference.md`` documents the request body
            for this endpoint and the ``route: null``/``waypoint_order``/
            ``available_capabilities`` fields, but doesn't show a full
            worked example of a non-null ``route`` object's own fields.
            ``distance_meters``/``duration_seconds`` here follow the naming
            convention used everywhere else in that same API (e.g. the
            buildings endpoint's own ``distance_meters``) - verify against a
            live REData instance once its routing endpoint is confirmed
            deployed, and adjust if its real field names differ.
        """
        body = self.post_json(
            "/api/v1/routes/",
            {
                "waypoints": [[latitude, longitude] for latitude, longitude in waypoints],
                "capability": capability,
                "profile": profile,
                "include_geometry": False,
            },
        )
        if not isinstance(body, dict):
            return None
        route = body.get("route")
        if not isinstance(route, dict):
            return None
        distance_meters = route.get("distance_meters")
        duration_seconds = route.get("duration_seconds")
        if distance_meters is None or duration_seconds is None:
            return None
        return {"distance_meters": distance_meters, "duration_seconds": duration_seconds}
