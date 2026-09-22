"""Raster basemap tiles, fetched from the vendor rather than through REData."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway, read_capped
from urbanlens.dashboard.services.map.basemap_vendors import vendor_for

#: Sent because a bare urllib/requests agent is what free vendor CDNs refuse first. This site's
#: blanket `Referrer-Policy: no-referrer` is not in play here - the fetch is server-side, so no
#: Referer is involved and no viewer's coordinates travel with it.
_USER_AGENT = "UrbanLens/1.0 (+https://urbanlens.org; basemap tile proxy)"


class BasemapVendorTilesGateway(Gateway):
    """Fetches one raster tile straight from the vendor that publishes it."""

    service_key: ClassVar[str] = "basemap_vendor_tiles"

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """Record the vendor and service, never the tile coordinate.

        Which tiles a viewer asked for is which places they were looking at, so the coordinate must
        not reach ``ApiCallLog`` any more than it reaches the vendor's own logs with a Referer.

        Args:
            url: The tile URL about to be requested.

        Returns:
            The URL truncated before the coordinate.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Esri paths carry the service name before `/tile/<z>/<y>/<x>`; the XYZ vendors carry
        # nothing but the coordinate, so the host alone identifies them.
        service, marker, _ = parsed.path.partition("/tile/")
        if marker:
            return f"{parsed.scheme}://{parsed.netloc}{service}/tile/"
        return f"{parsed.scheme}://{parsed.netloc}/"

    def download_tile(self, layer: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
        """Fetch one basemap tile from its vendor.

        Args:
            layer: Layer id from REData's catalogue.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            ``(status_code, body, content_type)``.

        Raises:
            ValueError: The layer has no vendor endpoint; the caller should use REData.
        """
        vendor = vendor_for(layer)
        if vendor is None:
            raise ValueError(f"No vendor endpoint for basemap layer {layer!r}")
        response = self.session.get(vendor.url_for(z, x, y), headers={"User-Agent": _USER_AGENT}, timeout=30, stream=True)
        return response.status_code, read_capped(response, what="basemap tile"), response.headers.get("Content-Type", "image/png")
