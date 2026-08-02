"""Unit conversion and formatting helpers for user-facing distances.

Distances are computed and stored internally in kilometres; these helpers convert
to the viewer's preferred unit (see ``Profile.effective_distance_units``) only at
display time.
"""

from __future__ import annotations

from urbanlens.dashboard.models.profile.meta import DistanceUnit

_MILES_PER_KM = 0.621371


def km_to_display(distance_km: float, units: str) -> float:
    """Convert a kilometre value to the given display unit.

    Args:
        distance_km: Distance in kilometres.
        units: A ``DistanceUnit`` value ("km" or "mi").

    Returns:
        The distance expressed in the requested unit.
    """
    if units == DistanceUnit.MILES:
        return distance_km * _MILES_PER_KM
    return distance_km


def unit_label(units: str) -> str:
    """Return the short label ("km" or "mi") for a ``DistanceUnit`` value."""
    return DistanceUnit.MILES.value if units == DistanceUnit.MILES else DistanceUnit.KILOMETERS.value


def format_distance(distance_km: float, units: str, *, decimals: int = 1) -> str:
    """Format a kilometre distance as a display string in the chosen unit.

    Args:
        distance_km: Distance in kilometres.
        units: A ``DistanceUnit`` value ("km" or "mi").
        decimals: Number of decimal places to render.

    Returns:
        A string like ``"12.3 km"`` or ``"7.6 mi"``.
    """
    value = km_to_display(distance_km, units)
    return f"{value:.{decimals}f} {unit_label(units)}"
