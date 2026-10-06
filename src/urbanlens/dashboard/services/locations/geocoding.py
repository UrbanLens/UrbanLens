"""Forward geocoding (free-text address -> coordinates).
Split out from ``controllers.maps`` so it can be shared by the map "add pin by address" flow and any other pin-creation entry point (e.g. the external API) without a controller-to-controller import."""

from __future__ import annotations

from urbanlens.dashboard.services.apis.locations.geocode_resolution import geocode_address


def get_pin_by_address(address: str) -> tuple[float | None, float | None]:
    """Resolve a free-text address to coordinates.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when the address doesn't resolve to a place.

    Raises:
        GatewayRequestError: The address could not be looked up: REData could not answer and the direct fallback was
            not asked (off production), the Nominatim budget refused it, or this environment does not call Nominatim."""
    return geocode_address(address)
