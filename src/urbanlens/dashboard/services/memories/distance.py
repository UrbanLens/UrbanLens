"""Total travel-distance computation for the Memories page.

"Distance traveled" combines two sources:

1. **Recorded routes** - the geodesic length of every imported/recorded Route.
2. **Travel between visits** - the great-circle distance between each pair of
   consecutive PinVisits (ordered chronologically). If a user logged a visit in
   New York, then California, then Oregon, this adds NY->CA plus CA->OR even
   though no route was recorded for those legs.

All values are returned in kilometres; convert to the viewer's unit at display
time with ``services.units``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Sum
from django.db.models.functions import Coalesce

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

    Visits are ordered by ``visited_at`` and use each pin's effective coordinates
    (the pin's own override, falling back to its location). Visits without any
    resolvable coordinate are skipped, and the leg is measured between the two
    nearest coordinate-bearing visits so a single gap does not break the chain.

    Args:
        profile: The profile whose visit history to measure.

    Returns:
        Total point-to-point travel distance across the visit sequence, in km.
    """
    coords = (
        PinVisit.objects.filter(pin__profile=profile)
        .annotate(
            _eff_lat=Coalesce("pin__latitude", "pin__location__latitude"),
            _eff_lng=Coalesce("pin__longitude", "pin__location__longitude"),
        )
        .filter(_eff_lat__isnull=False, _eff_lng__isnull=False)
        .order_by("visited_at")
        .values_list("_eff_lat", "_eff_lng")
    )

    total_km = 0.0
    previous: tuple[float, float] | None = None
    for lat, lng in coords:
        current = (float(lat), float(lng))
        if previous is not None:
            total_km += _haversine_km(previous, current)
        previous = current
    return total_km


def total_travel_distance_km(profile: Profile) -> float:
    """Return recorded-route distance plus travel between consecutive visits, in km."""
    return recorded_route_distance_km(profile) + inter_visit_distance_km(profile)
