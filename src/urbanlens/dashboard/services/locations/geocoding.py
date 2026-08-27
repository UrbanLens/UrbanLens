"""Forward geocoding (free-text address -> coordinates).

Split out from ``controllers.maps`` so it can be shared by the map "add pin by
address" flow and any other pin-creation entry point (e.g. the external API)
without a controller-to-controller import.
"""

from __future__ import annotations

from urbanlens.dashboard.services.apis.locations.geocode_resolution import geocode_address


def get_pin_by_address(address: str) -> tuple[float | None, float | None]:
    """Resolve a free-text address to coordinates.

    Tries REData first when configured, then falls back to the direct
    geopy/Nominatim gateway - see
    ``services.apis.locations.geocode_resolution``.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when the
        address doesn't resolve to a place.

    Raises:
        RateLimitExceededError: The app-wide Nominatim budget refused the
            direct fallback call.
    """
    return geocode_address(address)
