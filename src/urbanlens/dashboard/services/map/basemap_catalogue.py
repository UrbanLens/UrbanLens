"""The basemap layers this deployment can offer, from REData's tile catalogue.

Two consumers, one builder: ``BasemapTileCatalogueView`` serves this over HTTP for a client that
asks after the page has loaded, and ``{% basemap_tile_catalogue %}`` embeds it in the document so
the browser knows which tiles to draw *before* it draws any. The embed is the one that matters for
privacy: a map that paints vendor tiles first and swaps to this deployment's own afterwards has
already told that vendor which coordinates the user is looking at, which is what proxying them was
for (see ``frontend/ts/shared/map-layers.ts``'s note on ``Referrer-Policy: no-referrer``).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache

from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

logger = logging.getLogger(__name__)

#: The catalogue is documented as "called once per session"; a day keeps it
#: fresh enough to pick up a new layer without asking on every map load.
CATALOGUE_CACHE_TTL = 86400
CATALOGUE_CACHE_KEY = "ul_redata_tile_sources"


def tile_url_template(layer: str) -> str:
    """Leaflet-style template for one layer, pointing at this deployment's proxy.

    Args:
        layer: The layer id.

    Returns:
        A URL with literal ``{z}``/``{x}``/``{y}`` placeholders.
    """
    from django.urls import reverse

    # Sentinel coordinates rather than braces: reverse() would encode braces as %7Bz%7D, handing the client a
    # template it cannot fill in. The values are chosen not to occur in a layer id.
    concrete = reverse("map.basemap_tiles", kwargs={"layer": layer, "z": 900001, "x": 900002, "y": 900003})
    return concrete.replace("900001", "{z}").replace("900002", "{x}").replace("900003", "{y}")


def basemap_tile_catalogue(*, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """REData's layer catalogue, rewritten into what a browser on this deployment can actually use.

    A raster entry's vendor ``url_template`` is deliberately not passed through: it needs REData's
    key, and handing the browser a template it cannot use would produce a layer that silently fails
    to load. A vector entry's ``style_url`` needs no key (see REData's ``D11``) and is passed
    through unchanged - the client fetches and renders that style document directly, and REData
    never sees a vector tile go by.

    Args:
        allow_fetch: Whether a cache miss may go to REData. False for anything rendering a page:
            that call costs ~1.45s (``P131``) and the embed appears on every page built on
            ``themes/base.html``, so a cold cache would otherwise put a REData round trip inside
            the render of a profile page that has no map on it at all. A miss simply yields no
            embed, and the client's own ``registerRedataLayers()`` fetch - which is asynchronous,
            and which the catalogue view answers - repopulates the cache for every later render.

    Returns:
        One entry per offered layer - empty when REData is unconfigured or unreachable, so a map
        simply keeps its built-in vendor layers.
    """
    from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured

    if not redata_configured():
        return []

    cached = cache.get(CATALOGUE_CACHE_KEY)
    if cached is not None:
        return list(cached)
    if not allow_fetch:
        return []

    try:
        sources = RedataBasemapTilesGateway().list_sources()
    except (LocationContextUnavailableError, RequestCancelledError, OSError) as exc:
        # Not cached: an unreachable catalogue must not cost this
        # deployment its extra layers for a day.
        logger.warning("REData tile catalogue unavailable: %s", exc)
        return []

    layers: list[dict[str, Any]] = []
    for source in sources:
        if not source.get("attribution"):
            # Attribution is not decorative - every vendor here requires it
            # on the rendered map, so a layer without it is not offered.
            continue
        # Absent on REData deployments that have not yet rolled out the source_type
        # contract (D11) - treat as raster, the only shape that ever existed before it.
        is_vector = source.get("source_type") == "vector"
        if is_vector and not source.get("style_url"):
            continue
        entry: dict[str, Any] = {
            "id": source["id"],
            "name": source.get("name") or source["id"],
            "source_type": "vector" if is_vector else "raster",
            "attribution": source.get("attribution") or "",
            "min_zoom": source.get("min_zoom"),
            "max_zoom": source.get("max_zoom"),
        }
        if is_vector:
            entry["style_url"] = source["style_url"]
        else:
            entry["url_template"] = tile_url_template(source["id"])
        layers.append(entry)
    if layers:
        cache.set(CATALOGUE_CACHE_KEY, layers, CATALOGUE_CACHE_TTL)
    # An empty catalogue is not cached.
    return layers


def catalogue_for_viewer(*, authenticated: bool, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """:func:`basemap_tile_catalogue`, filtered to what this viewer can actually fetch.

    A raster entry points at :class:`~urbanlens.dashboard.controllers.basemap_tiles.BasemapTileView`,
    which is login-required, so offering one to a signed-out visitor (a public share page) would
    swap a working vendor layer for a grid of 404s. A vector entry's ``style_url`` is fetched
    straight from REData with no key, so it is offered to everyone.

    Args:
        authenticated: Whether the viewer is signed in to this deployment.
        allow_fetch: Passed through; see :func:`basemap_tile_catalogue`.

    Returns:
        The entries this viewer can load.
    """
    layers = basemap_tile_catalogue(allow_fetch=allow_fetch)
    if authenticated:
        return layers
    return [entry for entry in layers if entry.get("source_type") == "vector"]
