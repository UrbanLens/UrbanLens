"""Total travel-distance computation for the Memories page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Sum

from urbanlens.dashboard.models.profile.model import _haversine_km
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.models.visits.model import PinVisit

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def recorded_route_distance_km(profile: Profile) -> float:
    """Return the total geodesic length of the profile's recorded routes, in km."""
    total_m = Route.objects.for_profile(profile).aggregate(total=Sum("distance_meters"))["total"] or 0.0
    return total_m / 1000.0


def inter_visit_distance_km(profile: Profile) -> float:
    """Return the summed great-circle distance between consecutive visits, in km.
    Any visit without a resolvable coordinate is skipped, and the leg is measured between the two nearest coordinate-bearing visits so a single gap does not break the chain.

    Args:
        profile: The profile whose visit history to measure.

    Returns:
        Total point-to-point travel distance across the visit sequence, in km."""
    coords = PinVisit.objects.filter(pin__profile=profile).order_by("visited_at").values_list("pin__location__latitude", "pin__location__longitude")

    total_km = 0.0
    previous: tuple[float, float] | None = None
    for lat, lng in coords:
        if lat is None or lng is None:
            continue
        current = (float(lat), float(lng))
        if previous is not None:
            total_km += _haversine_km(previous, current)
        previous = current
    return total_km


def total_travel_distance_km(profile: Profile) -> float:
    """Return recorded-route distance plus travel between consecutive visits, in km."""
    return recorded_route_distance_km(profile) + inter_visit_distance_km(profile)
