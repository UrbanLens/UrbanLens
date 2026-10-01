"""Where a proxied tile lives in the cache, spelled once.

Both tile proxies and the load-run seeder address the same bytes, and they addressed them by each
writing the same f-string. A seeder that drifts from a proxy is invisible - the map still draws,
because a miss simply goes upstream - so it surfaces as a capacity run measuring the fetch path it
was seeded to avoid, which is what happened when the bytes moved to their own store.

These are keys only. What store they are read from and written to is
:mod:`~urbanlens.dashboard.services.core.bounded_cache`'s to decide.
"""

from __future__ import annotations

from hashlib import sha256


def basemap_tile_cache_key(layer: str, z: int, x: int, y: int) -> str:
    """Cache key for one proxied basemap tile.

    Carries the vendor's fingerprint, so repointing a layer at a different endpoint reads as a miss
    rather than serving the previous vendor's bytes for the rest of their week-long TTL. See
    ``VendorTiles.cache_tag``.

    Args:
        layer: The catalogue layer id.
        z: Tile zoom level.
        x: Tile column.
        y: Tile row.

    Returns:
        The cache key.
    """
    from urbanlens.dashboard.services.map.basemap_vendors import vendor_for

    vendor = vendor_for(layer)
    # `redata` for a layer this deployment does not fetch itself - its bytes cannot change vendor
    # without REData's catalogue changing too.
    tag = vendor.cache_tag if vendor else "redata"
    return f"ul_basemap_tile_{layer}_{tag}_{z}_{x}_{y}"


def vector_tile_cache_key(z: int, x: int, y: int) -> str:
    """Cache key for one proxied Protomaps vector tile.

    No layer in the key: the hosted API serves one pyramid for every theme - ``light`` and ``dark``
    differ in the style document, not the bytes - so keying per layer would buy the same tile twice.

    Args:
        z: Tile zoom level.
        x: Tile column.
        y: Tile row.

    Returns:
        The cache key.
    """
    return f"ul_pmtile_{z}_{x}_{y}"


def vector_style_cache_key(theme: str, origin: str) -> str:
    """Cache key for one proxied Protomaps style document.

    Keyed by origin as well as theme: the document names its tile endpoint absolutely, so a
    deployment reachable on more than one host must not serve one host's document to the other.

    Args:
        theme: The Protomaps theme name.
        origin: The scheme-and-host the document's tile URLs point at.

    Returns:
        The cache key.
    """
    return f"ul_pmstyle_{theme}_{sha256(origin.encode()).hexdigest()[:16]}"


def historical_tile_cache_key(georeference_uuid: str, z: int, x: int, y: int) -> str:
    """Cache key for one warped historical-map overlay tile.

    Args:
        georeference_uuid: REData georeference whose pyramid the tile comes from.
        z: Tile zoom level.
        x: Tile column.
        y: Tile row.

    Returns:
        The cache key.
    """
    return f"ul_histmap_tile_{georeference_uuid}_{z}_{x}_{y}"
