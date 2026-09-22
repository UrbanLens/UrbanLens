"""Where a raster basemap tile is actually fetched from.

REData publishes the basemap catalogue and serves the tiles, but none of the raster layers it
offers needs its API key - every one of them is a public, keyless vendor endpoint that REData is
itself fetching. Measured against ``k3s-staging`` in September 2026, going through it cost
0.36-0.56s for a satellite tile that Esri answers in 0.09s, and 1.24-1.33s for a terrain tile
OpenTopoMap answers in 0.36s. With ``basemap_tile_upstream_concurrency`` slots that is the
difference between ~6 tiles/sec and ~40, against a viewport of ~30.

So the proxy fetches the vendor directly for any layer named here, and falls back to REData for one
that is not. What the proxy does *not* do is hand these URLs to the browser: a map page URL can
encode a pin's coordinates, and which tiles a viewer asks for is which places they are looking at.
Keeping the fetch server-side is the whole reason the proxy exists (see
``frontend/ts/shared/map-layers.ts`` on ``Referrer-Policy: no-referrer``, and the 403s that policy
draws from free vendor CDNs when a browser asks them directly).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class VendorTiles:
    """One vendor's raster tile endpoint.

    Attributes:
        url_template: The vendor URL, with ``{z}``/``{x}``/``{y}`` placeholders and optionally
            ``{s}`` for a subdomain. Esri orders these ``{z}/{y}/{x}`` and the XYZ vendors
            ``{z}/{x}/{y}``; a template that swaps them serves a real tile for the wrong place,
            which no status code reports.
        subdomains: Hostnames ``{s}`` may take, or empty when the template has no ``{s}``.
        attribution: Set only where this endpoint is a *different* vendor from the one REData
            publishes for the layer, whose credit the catalogue would otherwise carry. Showing one
            vendor's credit over another's bytes is a licence breach, not a cosmetic error, so the
            credit has to travel with the URL that decides the bytes.
    """

    url_template: str
    subdomains: tuple[str, ...] = ()
    attribution: str | None = None

    def url_for(self, z: int, x: int, y: int) -> str:
        """Fill this template in for one tile.

        Args:
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            The vendor URL to fetch.
        """
        url = self.url_template.format(z=z, x=x, y=y, s="{s}")
        if not self.subdomains:
            return url
        # Deterministic rather than round-robin, so a tile keeps going to the host that may
        # already have it warm.
        return url.replace("{s}", self.subdomains[(x + y) % len(self.subdomains)])


_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services"

#: REData layer id -> the endpoint REData would have fetched on our behalf. Mirrors its
#: ``TILE_PROVIDERS``; a layer absent here still goes through REData.
VENDOR_TILES: dict[str, VendorTiles] = {
    "street": VendorTiles(url_template=f"{_ESRI}/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}"),
    "satellite": VendorTiles(url_template=f"{_ESRI}/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}"),
    "borders": VendorTiles(url_template=f"{_ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{{z}}/{{y}}/{{x}}"),
    # Esri's dark canvas rather than CARTO's dark_all, which was the last CARTO reference left and
    # was never a deliberate choice here. Only the fallback either way: `dark` draws as a Protomaps
    # vector style wherever WebGL2 is available.
    "dark": VendorTiles(
        url_template=f"{_ESRI}/Canvas/World_Dark_Gray_Base/MapServer/tile/{{z}}/{{y}}/{{x}}",
        attribution="Esri, HERE, Garmin, © OpenStreetMap contributors, and the GIS User Community",
    ),
    "terrain": VendorTiles(url_template="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", subdomains=("a", "b", "c")),
}


def vendor_attribution(layer: str) -> str | None:
    """The credit this deployment must show for a layer, when it overrides REData's vendor.

    Args:
        layer: Layer id from REData's catalogue.

    Returns:
        The credit to show instead of REData's, or None to keep REData's.
    """
    vendor = VENDOR_TILES.get(layer)
    return vendor.attribution if vendor else None


def vendor_for(layer: str) -> VendorTiles | None:
    """The vendor endpoint for a layer, if this deployment fetches it directly.

    Args:
        layer: Layer id from REData's catalogue.

    Returns:
        The vendor's endpoint, or None when the layer should go through REData.
    """
    return VENDOR_TILES.get(layer)
