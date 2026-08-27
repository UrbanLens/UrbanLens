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
- REData not configured, or its request fails - falls back to a direct
  Nominatim search through :class:`NominatimGateway`, so the fallback is
  rate-limited, cost-logged and timeout-bounded like every other outbound
  call (it used to construct a raw geopy client under a tutorial user agent
  Nominatim's operators block - see the 2026-08-15 STATUS entry in
  ``docs/reports/2026-08-11-codebase-audit.md``).

Only used for the simple "resolve an address typed into a pin-creation form"
flow - the richer, OSM-metadata-heavy Nominatim reverse-geocode panel
(``plugins.builtin.nominatim``) is a different, deliberately direct-only
integration (see that module's own docstring).
"""

from __future__ import annotations

import logging

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.apis.locations.redata_geocode_gateway import RedataGeocodeGateway
from urbanlens.dashboard.services.security.redact import redact_coordinate, redact_text

logger = logging.getLogger(__name__)


def geocode_address(address: str) -> tuple[float | None, float | None]:
    """Resolve a free-text address to coordinates, trying REData then direct Nominatim.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when the
        address doesn't resolve to a place anywhere.

    Raises:
        RateLimitExceededError: The app-wide Nominatim budget refused the
            fallback call (see :func:`nominatim_geocode`).
    """
    if redata_configured():
        try:
            envelope = RedataGeocodeGateway().geocode(address, limit=1)
        except LocationContextUnavailableError as exc:
            logger.warning("REData geocode failed for an address lookup, falling back to direct Nominatim: %s", exc.reason)
        else:
            if envelope.results:
                result = envelope.results[0]
                latitude, longitude = result.get("latitude"), result.get("longitude")
                if latitude is not None and longitude is not None:
                    return float(latitude), float(longitude)
            return None, None

    return nominatim_geocode(address)


def nominatim_geocode(address: str) -> tuple[float | None, float | None]:
    """Forward-geocode one address through the project's Nominatim gateway.

    The shared direct-Nominatim fallback (also used by
    ``controllers.settings.geocode_address``). Goes through
    :class:`~urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway`
    rather than a raw geopy client so the call is rate-limited (Nominatim's
    usage policy is one request/second; the app-wide budget enforces it),
    cost-logged, timeout-bounded, and sent under the project's own user agent.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when Nominatim
        has no such place or the request failed (the gateway flattens
        failures to an empty result).

    Raises:
        RateLimitExceededError: The app-wide Nominatim budget refused the
            call - propagated so a caller cannot mistake "we did not ask"
            for "no such place".
    """
    from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

    results = NominatimGateway().search(address, limit=1)
    if results:
        first = results[0]
        latitude, longitude = first.get("lat"), first.get("lon")
        if latitude is not None and longitude is not None:
            try:
                return float(latitude), float(longitude)
            except (TypeError, ValueError):
                logger.warning(
                    "Nominatim returned unparseable coordinates for %s: %s, %s",
                    redact_text(address),
                    redact_coordinate(latitude),
                    redact_coordinate(longitude),
                )
    return (None, None)
