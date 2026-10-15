"""
Where a proxied tile lives in the cache, spelled once.

The tile proxies and the load-run seeder address the same bytes, and they addressed them by each
writing the same f-string. A seeder that drifts from a proxy is invisible - the map still draws,
because a miss simply goes upstream - so it surfaces as a capacity run measuring the fetch path it
was seeded to avoid, which is what happened when the bytes moved to their own store.

These are keys only. What store they are read from and written to is
:mod:`~urbanlens.dashboard.services.core.bounded_cache`'s to decide.
"""

from __future__ import annotations


def basemap_tile_cache_key(layer: str, z: int, x: int, y: int) -> str:
    """
    Cache key for one proxied basemap tile.

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
    from urbanlens.dashboard.services.map.basemap_catalogue import active_tile_catalogue
    from urbanlens.dashboard.services.map.basemap_vendors import vendor_for

    vendor = vendor_for(layer)
    # The catalogue's own tag for a layer this deployment does not fetch itself - its bytes cannot change vendor
    # without the catalogue changing too.
    catalogue = active_tile_catalogue()
    tag = vendor.cache_tag if vendor else (catalogue.cache_tag if catalogue else "catalogue")
    return f"ul_basemap_tile_{layer}_{tag}_{z}_{x}_{y}"
