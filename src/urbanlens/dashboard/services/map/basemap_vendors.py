"""Where a raster basemap tile is actually fetched from.

REData publishes the basemap catalogue and serves the tiles, but none of the raster layers it
offers needs its API key - every one of them is a public, keyless vendor endpoint that REData is
itself fetching. Measured from a ``k3s-staging`` pod in September 2026 over 8 cold coordinates a
side, a satellite tile cost 0.490s through REData against 0.238s from Esri. Against a viewport of
~30 tiles and a handful of upstream slots, halving the per-tile cost is the difference between
filling and timing out.

So the proxy fetches the vendor directly for any layer named here, and falls back to REData for one
that is not. What the proxy does *not* do is hand these URLs to the browser: a map page URL can
encode a pin's coordinates, and which tiles a viewer asks for is which places they are looking at.
Keeping the fetch server-side is the whole reason the proxy exists (see
``frontend/ts/shared/map-layers.ts`` on ``Referrer-Policy: no-referrer``, and the 403s that policy
draws from free vendor CDNs when a browser asks them directly).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re


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
        max_native_zoom: Deepest level this endpoint holds real tiles for, where that differs from
            the depth REData publishes for the layer. A layer named here never reaches REData, so
            its published depth describes an endpoint that is not being used: too shallow and the
            client upscales levels this vendor would have drawn, too deep and the proxy fetches
            Esri's blank-past-coverage JPEG, which is a 200 and caches for a week like any tile.
        browser_max_age: The longest the vendor lets a client keep a tile, from its own
            ``Cache-Control``; the browser is told no longer than this.
    """

    url_template: str
    subdomains: tuple[str, ...] = ()
    attribution: str | None = None
    max_native_zoom: int | None = None
    browser_max_age: int | None = None

    @property
    def cache_tag(self) -> str:
        """Short fingerprint of this endpoint, for cache keys and the published tile URL.

        A tile is held for a week by layer and coordinate, in this deployment's own store and again
        at the CDN. Neither key would otherwise say which vendor produced the bytes, so repointing a
        layer keeps drawing the old vendor's tiles at whatever zooms are already cached.

        Returns:
            Eight hex characters derived from the URL template.
        """
        return sha256(self.url_template.encode()).hexdigest()[:8]

    @property
    def esri_service(self) -> str | None:
        """The ArcGIS Online service these tiles come from, when they are Esri's.

        Esri credits each basemap's data providers by area and zoom, and publishes which ones apply
        where at ``static.arcgis.com/attribution/<service>``. The browser asks that for the view it
        is showing, so it needs the service's name - which a proxied tile URL no longer carries.

        Returns:
            A service path such as ``World_Imagery`` or ``Reference/World_Boundaries_and_Places``,
            or None for a vendor that is not Esri.
        """
        match = _ESRI_SERVICE.match(self.url_template)
        return match.group(1) if match else None

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
#: What Esri sends with a tile: ``Cache-Control: max-age=86400``, read off World_Imagery and
#: World_Street_Map on 2026-10-06.
_ESRI_MAX_AGE = 86_400

#: The service path inside an ArcGIS Online tile URL - ``Canvas/World_Dark_Gray_Base`` keeps its folder.
_ESRI_SERVICE = re.compile(r"^https://(?:server|services)\.arcgisonline\.com/ArcGIS/rest/services/(.+?)/MapServer/", re.IGNORECASE)

#: REData layer id -> the endpoint REData would have fetched on our behalf. Mirrors its
#: ``TILE_PROVIDERS``; a layer absent here still goes through REData.
VENDOR_TILES: dict[str, VendorTiles] = {
    "street": VendorTiles(url_template=f"{_ESRI}/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}", browser_max_age=_ESRI_MAX_AGE),
    "satellite": VendorTiles(url_template=f"{_ESRI}/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}", browser_max_age=_ESRI_MAX_AGE),
    "borders": VendorTiles(url_template=f"{_ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{{z}}/{{y}}/{{x}}", browser_max_age=_ESRI_MAX_AGE),
    # Esri's dark canvas rather than CARTO's dark_all, which was the last CARTO reference left and
    # was never a deliberate choice here. Only the fallback either way: `dark` draws as a Protomaps
    # vector style wherever WebGL2 is available.
    "dark": VendorTiles(
        url_template=f"{_ESRI}/Canvas/World_Dark_Gray_Base/MapServer/tile/{{z}}/{{y}}/{{x}}",
        attribution="Esri, HERE, Garmin, © OpenStreetMap contributors, and the GIS User Community",
        browser_max_age=_ESRI_MAX_AGE,
    ),
    # Esri's topographic map rather than OpenTopoMap, which measured 0.566s a tile against 0.25s
    # here. It is the full map - contours, roads and labels - rather than the bare relief of
    # `World_Hillshade`, which is meant to go *under* a map and renders near-white over flat or
    # urban ground. Credit trimmed to the principal sources: Esri's `copyrightText` names 18 and
    # overflows the footer.
    # 19 rather than the 17 REData publishes for OpenTopoMap, which is where this map stopped
    # drawing detail: measured real tiles through 19 and the blank at 20, matching the depth the
    # other Esri rasters here are already published at.
    "terrain": VendorTiles(
        url_template=f"{_ESRI}/World_Topo_Map/MapServer/tile/{{z}}/{{y}}/{{x}}",
        attribution="Esri, HERE, Garmin, Intermap, USGS, NPS, © OpenStreetMap contributors, and the GIS User Community",
        max_native_zoom=19,
        browser_max_age=_ESRI_MAX_AGE,
    ),
}


def vendor_for(layer: str) -> VendorTiles | None:
    """The vendor endpoint for a layer, if this deployment fetches it directly.

    Args:
        layer: Layer id from REData's catalogue.

    Returns:
        The vendor's endpoint, or None when the layer should go through REData.
    """
    return VENDOR_TILES.get(layer)
