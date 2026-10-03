"""Web Mercator (slippy-map) ground resolution."""

from __future__ import annotations

import math

#: Metres a pixel covers at zoom 0 on the equator, for 256-pixel tiles.
METERS_PER_PIXEL_AT_ZOOM_0 = 156_543.03392
MAX_ZOOM = 22


def meters_per_pixel(latitude: float, zoom: float) -> float:
    """Ground distance one pixel covers at *zoom* and *latitude*.

    Args:
        latitude: WGS-84 latitude in degrees.
        zoom: Slippy-map zoom level.

    Returns:
        Metres per pixel.
    """
    return METERS_PER_PIXEL_AT_ZOOM_0 * math.cos(math.radians(latitude)) / 2**zoom


def native_zoom(resolution_meters: float, latitude: float) -> int:
    """The deepest zoom whose pixels are no finer than a source's own resolution, so its tiles aren't enlarged.

    Args:
        resolution_meters: The source's ground sample distance.
        latitude: WGS-84 latitude in degrees.

    Returns:
        A zoom in ``[0, MAX_ZOOM]``.

    Raises:
        ValueError: *resolution_meters* is not a positive, finite number.
    """
    if not (math.isfinite(resolution_meters) and resolution_meters > 0):
        raise ValueError(f"resolution_meters must be positive and finite, not {resolution_meters!r}")
    # The tolerance keeps a resolution equal to a zoom's own scale at that zoom despite float rounding.
    zoom = math.floor(math.log2(meters_per_pixel(latitude, 0) / resolution_meters) + 1e-9)
    return max(0, min(MAX_ZOOM, zoom))
