"""Drive-time legs between consecutive trip activities (UL-60 slice).

Routed with the OSRM gateway and cached aggressively: road distances between
two fixed places don't change, so a leg is fetched live at most once and then
served from cache for weeks. Renders are budgeted - a panel render performs at
most a couple of live routing calls and simply omits the legs it couldn't
fetch yet; the next render fills them in from a warmer cache. This keeps the
activities panel fast even for long itineraries, and keeps OSRM usage inside
its (self-imposed) rate limits.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import logging

from django.core.cache import cache

from urbanlens.dashboard.services.apis.routing.osrm import OSRMGateway
from urbanlens.dashboard.services.gateway import GatewayRequestError

logger = logging.getLogger(__name__)

#: Successful legs are effectively static - keep them for two weeks.
_LEG_CACHE_TTL = 14 * 24 * 60 * 60
#: Unroutable pairs (ferry-only, disconnected networks) retry daily.
_UNROUTABLE_CACHE_TTL = 24 * 60 * 60
#: Live OSRM calls allowed per panel render.
_MAX_LIVE_CALLS = 2

_UNROUTABLE = {"unroutable": True}


@dataclass(slots=True, frozen=True)
class TripLeg:
    """One driving leg between two consecutive activities."""

    distance_meters: float
    duration_seconds: float

    @property
    def duration_display(self) -> str:
        """Human duration: ``"38 min"`` / ``"2 hr 5 min"`` / ``"3 hr"``."""
        minutes = round(self.duration_seconds / 60)
        if minutes < 1:
            return "1 min"
        hours, minutes = divmod(minutes, 60)
        if not hours:
            return f"{minutes} min"
        return f"{hours} hr {minutes} min" if minutes else f"{hours} hr"

    @property
    def distance_display(self) -> str:
        """Human distance in miles: ``"18.3 mi"`` / ``"142 mi"``."""
        miles = self.distance_meters * 0.000621371
        if miles >= 100:
            return f"{round(miles)} mi"
        return f"{miles:.1f} mi"


def _cache_key(origin: tuple[float, float], destination: tuple[float, float]) -> str:
    return f"dashboard:trip_leg:v1:driving:{origin[0]:.5f},{origin[1]:.5f}:{destination[0]:.5f},{destination[1]:.5f}"


def compute_legs(sequence: list[tuple[int, tuple[float, float]]], *, max_live_calls: int = _MAX_LIVE_CALLS) -> dict[int, TripLeg]:
    """Compute driving legs between consecutive stops in ``sequence``.

    Args:
        sequence: Ordered ``(activity_id, (latitude, longitude))`` stops -
            only include stops whose coordinates the viewer may see.
        max_live_calls: Budget of uncached OSRM requests this call may make;
            legs beyond the budget are omitted (not errors) until a later,
            warmer render.

    Returns:
        Mapping of the *arriving* activity's id to its leg from the previous
        stop. Unroutable or not-yet-fetched pairs are absent.
    """
    legs: dict[int, TripLeg] = {}
    gateway: OSRMGateway | None = None
    live_calls = 0

    for (_, origin), (activity_id, destination) in itertools.pairwise(sequence):
        if origin == destination:
            continue
        key = _cache_key(origin, destination)
        cached = cache.get(key)
        if cached == _UNROUTABLE:
            continue
        if cached is None:
            if live_calls >= max_live_calls:
                continue
            live_calls += 1
            if gateway is None:
                gateway = OSRMGateway()
            try:
                cached = gateway.get_route_between(origin, destination)
            except GatewayRequestError:
                logger.debug("OSRM leg lookup rate-limited/unavailable", exc_info=True)
                continue
            if cached is None or cached.get("duration_seconds") is None or cached.get("distance_meters") is None:
                cache.set(key, _UNROUTABLE, _UNROUTABLE_CACHE_TTL)
                continue
            cache.set(key, cached, _LEG_CACHE_TTL)
        legs[activity_id] = TripLeg(distance_meters=cached["distance_meters"], duration_seconds=cached["duration_seconds"])

    return legs
