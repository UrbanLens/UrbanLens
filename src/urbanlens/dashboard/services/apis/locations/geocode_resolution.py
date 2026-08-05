"""Resolves forward-geocoding (address -> coordinates) to REData or direct Nominatim.

Single chokepoint for the "which provider answers a forward-geocode call"
decision, mirroring ``cid_resolution.py``/``places_resolution.py``/
``weather_resolution.py``/``routing_resolution.py``'s precedent:

- REData configured (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` both set) -
  the primary deployment's path. ``GET /geocode/`` dispatches across every
  registered provider (nominatim/photon/google/azure_maps/openhistoricalmap)
  in one call; the first result of the first provider REData lists is used,
  matching the "take the first result of the first provider you trust"
  guidance in REData's own ``docs/api-reference.md``.
- REData not configured, or its request fails - falls back to the existing
  direct geopy/Nominatim gateway (``services.locations.geocoding``),
  preserving today's behavior exactly.

Only used for the simple "resolve an address typed into a pin-creation form"
flow - the richer, OSM-metadata-heavy Nominatim reverse-geocode panel
(``plugins.builtin.nominatim``) is a different, deliberately direct-only
integration (see that module's own docstring).
"""

from __future__ import annotations

import logging

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.apis.locations.redata_geocode_gateway import RedataGeocodeGateway

logger = logging.getLogger(__name__)


def geocode_address(address: str) -> tuple[float | None, float | None]:
    """Resolve a free-text address to coordinates, trying REData then direct Nominatim.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when the
        address doesn't resolve to a place anywhere.

    Raises:
        GeocoderTimedOut: The direct Nominatim fallback didn't respond in time.
        GeocoderUnavailable: The direct Nominatim fallback is unreachable.
    """
    if redata_configured():
        try:
            envelope = RedataGeocodeGateway().geocode(address, limit=1)
        except LocationContextUnavailableError as exc:
            logger.warning("REData geocode failed for an address lookup, falling back to direct Nominatim: %s", exc)
        else:
            if envelope.results:
                result = envelope.results[0]
                latitude, longitude = result.get("latitude"), result.get("longitude")
                if latitude is not None and longitude is not None:
                    return float(latitude), float(longitude)
            return None, None

    from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
    from geopy.geocoders import Nominatim

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
