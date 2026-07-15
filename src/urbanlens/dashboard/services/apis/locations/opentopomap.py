"""OpenTopoMap gateway - free, open-source, keyless topographic raster tiles.

https://opentopomap.org/ - SRTM elevation data rendered over OpenStreetMap,
useful for the satellite/aerial carousel's terrain/trail context (contour
lines, trail markings) that pure aerial photography doesn't show. No API key
is required, and since there's nothing to hide, the tile is loaded directly
by the browser rather than proxied server-side (see ``NasaGibsGateway`` for
the same keyless-direct-URL pattern).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider

if TYPE_CHECKING:
    from collections.abc import Generator

#: OpenTopoMap tile server (round-robins a/b/c subdomains per OSMF tile usage policy).
_TILE_URL = "https://a.tile.opentopomap.org/{zoom}/{x}/{y}.png"


def _lonlat_to_tile(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    """Web Mercator lon/lat -> slippy-map tile (x, y) at a given zoom."""
    latitude = max(min(latitude, 85.05112878), -85.05112878)
    lat_rad = math.radians(latitude)
    n = 2**zoom
    x = int((longitude + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


@dataclass(slots=True, kw_only=True)
class OpenTopoMapGateway(SatelliteViewProvider):
    """Gateway for OpenTopoMap's public tile server.

    Provides current topographic map tiles (SRTM contours over OSM) as
    ``SatelliteSlide`` objects. No API key is required.
    """

    service_key: ClassVar[str] = "opentopomap"
    paid_service: ClassVar[bool] = False

    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 15,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        """Yield the OpenTopoMap tile covering a coordinate as a SatelliteSlide.

        A single 256x256 slippy-map tile, not a perfectly centered composite
        image like the other satellite providers - good enough for terrain/
        trail context, not pixel-precise framing.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.
            zoom: Slippy-map zoom level (14-16 shows trail-level detail).

        Yields:
            A single SatelliteSlide whose ``img_src`` the browser fetches
            directly (no key to hide, so no server-side proxying needed).
        """
        x, y = _lonlat_to_tile(longitude, latitude, zoom)
        yield SatelliteSlide(
            img_src=_TILE_URL.format(zoom=zoom, x=x, y=y),
            source="OpenTopoMap",
            date="Current",
            detail="Topographic map - SRTM contours and trails over OpenStreetMap",
        )
