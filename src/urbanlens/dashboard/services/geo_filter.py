"""Geographic filtering utilities.

Provides ``is_usa_coordinates`` to determine whether a (lat, lng) pair falls
within United States territory.  Used to guard USA-centric API services (NPS,
LoopNet, Library of Congress, etc.) so calls are never wasted on non-US
locations.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _BBox:
    lat_min: float
    lat_max: float
    lng_min: float
    lng_max: float

    def contains(self, lat: float, lng: float) -> bool:
        """Return True if (lat, lng) falls within this bounding box."""
        return self.lat_min <= lat <= self.lat_max and self.lng_min <= lng <= self.lng_max


# Approximate bounding boxes for US territories.
# These are intentionally generous - false *negatives* (blocking a US location)
# are worse than false *positives* (allowing a near-miss).
_USA_BBOXES: tuple[_BBox, ...] = (
    # Continental United States (conterminous)
    _BBox(lat_min=24.396308, lat_max=49.384358, lng_min=-125.000000, lng_max=-66.934570),
    # Alaska (main landmass + western islands)
    _BBox(lat_min=54.800000, lat_max=71.538800, lng_min=-168.000000, lng_max=-130.000000),
    # Aleutian Islands (cross the anti-meridian; split into two boxes)
    _BBox(lat_min=51.200000, lat_max=54.800000, lng_min=-180.000000, lng_max=-130.000000),
    _BBox(lat_min=51.200000, lat_max=54.800000, lng_min=171.000000, lng_max=180.000000),
    # Hawaii
    _BBox(lat_min=18.910361, lat_max=22.235097, lng_min=-160.300000, lng_max=-154.806000),
    # Puerto Rico
    _BBox(lat_min=17.831509, lat_max=18.516766, lng_min=-67.942848, lng_max=-65.221909),
    # U.S. Virgin Islands
    _BBox(lat_min=17.678268, lat_max=18.412655, lng_min=-65.154389, lng_max=-64.512674),
    # Guam
    _BBox(lat_min=13.182397, lat_max=13.706179, lng_min=144.573975, lng_max=144.954937),
    # American Samoa
    _BBox(lat_min=-14.731771, lat_max=-14.159447, lng_min=-170.846497, lng_max=-169.416504),
    # Northern Mariana Islands
    _BBox(lat_min=14.036565, lat_max=20.616555, lng_min=144.813338, lng_max=146.154418),
)


def is_usa_coordinates(lat: float | None, lng: float | None) -> bool:
    """Return ``True`` if the given coordinates are within US territory.

    Accepts ``None`` inputs and returns ``False`` (no coordinates → cannot
    confirm US location → skip USA-only service).

    Args:
        lat: Latitude in WGS-84 decimal degrees.
        lng: Longitude in WGS-84 decimal degrees.

    Returns:
        ``True`` if within any US territory bounding box, ``False`` otherwise.
    """
    if lat is None or lng is None:
        return False
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return False

    return any(bbox.contains(lat_f, lng_f) for bbox in _USA_BBOXES)


def require_usa(service: str, lat: float | None, lng: float | None) -> bool:
    """Log a geo-filtered skip and return ``False`` if coordinates are outside the USA.

    Convenience wrapper for gateway methods that need to guard a USA-only call.
    Logs the skipped attempt via ``rate_limiter.log_api_call`` so it appears in
    the admin stats page.

    Args:
        service: The service key (e.g. ``"nps"``).
        lat: Latitude.
        lng: Longitude.

    Returns:
        ``True`` if the coordinates are in the USA (call may proceed),
        ``False`` if outside the USA (call should be skipped).
    """
    if is_usa_coordinates(lat, lng):
        return True

    import logging
    logging.getLogger(__name__).debug(
        "Skipping %s call: coordinates (%.4f, %.4f) are outside the USA",
        service, lat or 0, lng or 0,
    )
    from urbanlens.dashboard.services.rate_limiter import log_api_call
    log_api_call(service, success=True, was_geo_filtered=True)
    return False
