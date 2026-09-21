"""Where a proxied tile lives in the cache, spelled once.

Both tile proxies and the load-run seeder address the same bytes, and they addressed them by each
writing the same f-string. A seeder that drifts from a proxy is invisible - the map still draws,
because a miss simply goes upstream - so it surfaces as a capacity run measuring the fetch path it
was seeded to avoid, which is what happened when the bytes moved to their own store.

These are keys only. What store they are read from and written to is
:mod:`~urbanlens.dashboard.services.core.bounded_cache`'s to decide.
"""

from __future__ import annotations


def basemap_tile_cache_key(layer: str, z: int, x: int, y: int) -> str:
    """Cache key for one proxied basemap tile.

    Args:
        layer: The catalogue layer id.
        z: Tile zoom level.
        x: Tile column.
        y: Tile row.

    Returns:
        The cache key.
    """
    return f"ul_basemap_tile_{layer}_{z}_{x}_{y}"


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
