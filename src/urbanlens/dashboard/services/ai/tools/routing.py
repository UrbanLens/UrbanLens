"""The assistant's distance/drive-time tool - calls OSRM directly, never REData.

``services.apis.locations`` has a REData-first chokepoint for routing
(``routing_resolution``), the same shape as ``weather_resolution``'s for
weather - this deliberately bypasses it, straight to ``OSRMGateway``, so the
sandboxed AI worker's "no REData" guarantee (``docs/AI_PIPELINE.md``) holds
for this tool without needing REData reachable from it at all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.profile.meta import DistanceUnit
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register, remaining_deadline
from urbanlens.dashboard.services.core.units import km_to_display
from urbanlens.dashboard.services.geo.distance import haversine_km


def _resolve_point(context: ToolContext, pin_slug: str, lat: float | None, lng: float | None) -> tuple[float, float] | None:
    """A ``(lat, lng)`` endpoint from one of the requesting profile's own pins, or an explicit coordinate.

    Never resolves any other profile's pin - see ``Pin.objects.by_profile``.
    """
    pin_slug = pin_slug.strip()
    if pin_slug:
        from urbanlens.dashboard.models.pin.model import Pin

        pin = Pin.objects.by_profile(context.profile).filter(slug=pin_slug).select_related("location").first()
        if pin is None or pin.location is None:
            return None
        return float(pin.location.latitude), float(pin.location.longitude)
    if lat is not None and lng is not None:
        return lat, lng
    return None


class DistanceAndDriveTimeArgs(BaseModel):
    from_pin_slug: str = Field(default="", max_length=255)
    to_pin_slug: str = Field(default="", max_length=255)
    from_lat: float | None = Field(default=None, ge=-90, le=90)
    from_lng: float | None = Field(default=None, ge=-180, le=180)
    to_lat: float | None = Field(default=None, ge=-90, le=90)
    to_lng: float | None = Field(default=None, ge=-180, le=180)


def _distance_and_drive_time(context: ToolContext, args: DistanceAndDriveTimeArgs) -> dict[str, Any]:
    from urbanlens.dashboard.services.apis.routing.osrm import OSRMGateway
    from urbanlens.dashboard.services.core.timeout_utils import call_with_deadline

    origin = _resolve_point(context, args.from_pin_slug, args.from_lat, args.from_lng)
    destination = _resolve_point(context, args.to_pin_slug, args.to_lat, args.to_lng)
    if origin is None or destination is None:
        return {"error": "Each endpoint needs either one of the user's own pins (from_pin_slug/to_pin_slug) or explicit from_lat/from_lng and to_lat/to_lng coordinates."}

    distance_km = haversine_km(*origin, *destination)
    units = context.profile.effective_distance_units
    result: dict[str, Any] = {
        "distance_km": round(distance_km, 1),
        "distance_mi": round(km_to_display(distance_km, DistanceUnit.MILES), 1),
        "preferred_unit": units,
        "source": "unavailable",
    }

    try:
        route = call_with_deadline(lambda: OSRMGateway().get_route_between(origin, destination), timeout=remaining_deadline(context), default=None, name="osrm")
    except Exception:
        route = None
    if route is not None and route.get("duration_seconds") is not None:
        result["drive_time_minutes"] = round(route["duration_seconds"] / 60)
        result["source"] = "osrm"
    return result


register(
    ToolSpec(
        name="distance_and_drive_time",
        description=(
            "Straight-line distance and (when routing succeeds) drive time between two points - each is one of the user's own "
            "pins (from_pin_slug/to_pin_slug) or explicit coordinates (from_lat/from_lng, to_lat/to_lng, e.g. the user's current "
            "location). source is 'osrm' when a real drive time was found, or 'unavailable' when only straight-line distance "
            "could be computed - never present straight-line distance as a drive time."
        ),
        args_model=DistanceAndDriveTimeArgs,
        handler=_distance_and_drive_time,
        features=frozenset({SiteFeature.AI}),
        requires_external_apis=True,
        scope=DataScope.OWN_PROFILE,
        progress_label="Calculating distance…",
        action_label="Calculated distance",
    ),
)
