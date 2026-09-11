"""Geographic filtering utilities.
The underlying boundary now lives in ``services.geo.geo_boundary`` (``USA``), the generalized replacement for this module's old standalone bbox check - a plugin that needs an arbitrary geographic gate (a different country, a state, a hand-drawn polygon) should use ``GeoBoundary`` directly rather than adding a new bespoke helper here."""

from __future__ import annotations

from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.security.redact import redact_coordinate


def is_usa_coordinates(lat: float | None, lng: float | None) -> bool:
    """Return ``True`` if the given coordinates are within US territory.
    Accepts ``None`` inputs and returns ``False`` (no coordinates → cannot confirm US location → skip USA-only service).

    Args:
        lat: Latitude in WGS-84 decimal degrees.
        lng: Longitude in WGS-84 decimal degrees.

    Returns:
        ``True`` if within any US territory bounding box, ``False`` otherwise."""
    return USA.contains(lat, lng)


def require_usa(service: str, lat: float | None, lng: float | None) -> bool:
    """Log a geo-filtered skip and return ``False`` if coordinates are outside the USA.
    Convenience wrapper for gateway methods that need to guard a USA-only call.

    Args:
        service: The service key (e.g. ``"nps"``).
        lat: Latitude.
        lng: Longitude.

    Returns:
        ``True`` if the coordinates are in the USA (call may proceed),
        ``False`` if outside the USA (call should be skipped)."""
    if is_usa_coordinates(lat, lng):
        return True

    import logging

    logging.getLogger(__name__).debug(
        "Skipping %s call: coordinates (%s, %s) are outside the USA",
        service,
        redact_coordinate(lat),
        redact_coordinate(lng),
    )
    from urbanlens.dashboard.services.core.rate_limiter import log_api_call

    log_api_call(service, success=True, was_geo_filtered=True)
    return False
