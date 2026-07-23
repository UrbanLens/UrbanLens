"""Forward geocoding (free-text address -> coordinates) via Nominatim.

Split out from ``controllers.maps`` so it can be shared by the map "add pin by
address" flow and any other pin-creation entry point (e.g. the external API)
without a controller-to-controller import.
"""

from __future__ import annotations

import logging

from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)


def get_pin_by_address(address: str) -> tuple[float | None, float | None]:
    """Resolve a free-text address to coordinates.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when the
        address doesn't resolve to a place.

    Raises:
        GeocoderTimedOut: The geocoding service didn't respond in time.
        GeocoderUnavailable: The geocoding service is unreachable.
    """
    try:
        geolocator = Nominatim(user_agent="geoapiExercises")
        pin = geolocator.geocode(address)
        if pin:
            return (pin.latitude, pin.longitude)
    except GeocoderTimedOut:
        logger.exception("Geocoder service timed out.")
        raise
    except GeocoderUnavailable:
        logger.exception("Geocoder service unavailable.")
        raise
    return (None, None)
