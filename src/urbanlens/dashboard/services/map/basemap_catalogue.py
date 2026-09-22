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
import re
import time
from typing import Any
from urllib.parse import quote

from django.core.cache import cache

from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.map.basemap_vendors import vendor_for

logger = logging.getLogger(__name__)

#: The catalogue is documented as "called once per session"; a day keeps it
#: fresh enough to pick up a new layer without asking on every map load.
CATALOGUE_CACHE_TTL = 86400
CATALOGUE_CACHE_KEY = "ul_redata_tile_sources"

#: Held for the length of one refill. Its TTL only has to outlive a gateway call that has
#: already spent the rate limiter's patience; the reservation is released on every exit.
CATALOGUE_FETCH_KEY = "ul_redata_tile_sources:fetching"
CATALOGUE_FETCH_TTL = 30

#: How long a caller that lost the reservation waits for the winner's answer before giving up
#: and rendering vendor layers. One call costs ~1.45s (``P131``).
CATALOGUE_WAIT_SECONDS = 3.0
_WAIT_POLL_SECONDS = 0.05

#: The proxy route captures ``<slug:layer>``, so this is the alphabet an id has to
#: be in to have a reachable URL at all.
_LAYER_ID = re.compile(r"[-a-zA-Z0-9_]+")


def tile_url_template(layer: str) -> str:
    """Leaflet-style template for one layer, pointing at this deployment's proxy.

    Args:
        layer: The layer id.

    Returns:
        A URL with literal ``{z}``/``{x}``/``{y}`` placeholders.
    """
    from django.urls import reverse

    # Sentinel coordinates rather than braces: reverse() would encode braces as %7Bz%7D, handing the client a
    # template it cannot fill in. Replaced from the right, because the route's alphabet for a layer id is
    # `[-a-zA-Z0-9_]` and the id comes from REData - so no numeric sentinel is one an id cannot contain, and
    # a layer called `route900002` would otherwise be published with `{x}` in the middle of its own name.
    concrete = reverse("map.basemap_tiles", kwargs={"layer": layer, "z": 900001, "x": 900002, "y": 900003})
    for sentinel, placeholder in (("900001", "{z}"), ("900002", "{x}"), ("900003", "{y}")):
        head, _, tail = concrete.rpartition(sentinel)
        concrete = f"{head}{placeholder}{tail}"
    # The vendor's fingerprint, so repointing a layer moves it to fresh URLs. A CDN keys on the URL
    # and these tiles are published `immutable` for a week, so without this a vendor swap keeps
    # being served the old vendor's tiles from the edge no matter what this origin now fetches -
    # and an edge purge is not something the catalogue can perform. The proxy ignores the value.
    vendor = vendor_for(layer)
    return f"{concrete}?v={vendor.cache_tag}" if vendor else concrete


def forget_basemap_tile_catalogue() -> None:
    """Drop the cached catalogue, so the next reader asks REData again rather than keeping a day of
    layers this deployment has since turned out to be unable to serve.

    A map draws what the catalogue named and nothing else, so a catalogue that outlives the
    deployment's ability to fetch those tiles is a grey map for as long as it is cached - where the
    vendor layers it replaced would have drawn. The proxy calls this when it is refused outright.
    """
    cache.delete(CATALOGUE_CACHE_KEY)


def _await_catalogue() -> list[dict[str, Any]]:
    """Wait out whoever holds the refill, rather than making a second identical call.

    Returns:
        The catalogue the holder published, or empty if it gave up or is still going.
    """
    deadline = time.monotonic() + CATALOGUE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_WAIT_POLL_SECONDS)
        cached = cache.get(CATALOGUE_CACHE_KEY)
        if cached is not None:
            return list(cached)
        if single_flight.holder(CATALOGUE_FETCH_KEY) is None:
            # The holder finished without publishing, so it failed or REData offers nothing.
            break
    return []


#: REData layer id -> theme in Protomaps' hosted style set. Only the two layers their basemap
#: covers; relief shading has no hosted vector equivalent, so terrain stays raster on both engines.
_PROTOMAPS_THEMES = {"street": "light", "dark": "dark"}


def protomaps_style_url(source_id: str) -> str | None:
    """Protomaps' hosted style for ``source_id``, when this deployment is configured to buy it.

    Chosen here rather than in REData so one catalogue serves both a deployment that self-hosts
    this basemap and one that pays Protomaps to host it: REData says which layers exist and what
    they may be credited as, this says where *this* deployment's browsers fetch the style from.
    Self-hosting stays the default - the key being unset is what selects it.

    The key reaches the browser, which is the documented shape for this API rather than a leak:
    Protomaps authorises a key against the ``Origin`` of the request, so one lifted from a page
    is refused everywhere but the sites its owner listed.

    Args:
        source_id: The REData layer id.

    Returns:
        The style URL, or None to keep whatever REData published.
    """
    from urbanlens.UrbanLens.settings.app import settings

    theme = _PROTOMAPS_THEMES.get(source_id)
    key = settings.protomaps_api_key
    if not theme or not key:
        return None
    return f"https://api.protomaps.com/styles/v5/{theme}/en.json?key={quote(key, safe='')}"


def _depth_of_the_bytes(published: Any, vendor_depth: int | None) -> Any:
    """How deep the layer goes, given who is actually being fetched.

    A layer named in the vendor table never reaches REData, so REData's depth describes an endpoint
    that is not being used - too shallow and the client upscales levels the vendor would have drawn,
    too deep and the proxy fetches Esri's blank-past-coverage JPEG, which is a 200 and caches like
    any other tile. So where the vendor declares a depth it is the one to publish, either way.

    Args:
        published: The depth REData published, which may be None or a non-integer.
        vendor_depth: The vendor's own deepest level, or None when it declares none.

    Returns:
        The depth to publish.
    """
    return published if vendor_depth is None else vendor_depth


def _offered_layers(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite REData's sources into the entries a browser on this deployment can use.

    Args:
        sources: REData's catalogue, as returned by the gateway.

    Returns:
        One entry per layer this deployment will offer.
    """
    layers: list[dict[str, Any]] = []
    for source in sources:
        source_id = source.get("id")
        if not isinstance(source_id, str) or not _LAYER_ID.fullmatch(source_id):
            # An id outside the route's alphabet has no proxy URL to build, and
            # reverse() answers that with NoReverseMatch rather than a skip - so
            # one such entry would otherwise 500 the whole catalogue.
            logger.warning("Skipping REData tile source with unusable id %r", source_id)
            continue
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
            "id": source_id,
            "name": source.get("name") or source_id,
            "source_type": "vector" if is_vector else "raster",
            "attribution": source.get("attribution") or "",
            "min_zoom": source.get("min_zoom"),
            "max_zoom": source.get("max_zoom"),
        }
        # Where this deployment fetches the raster from a different vendor than REData names, the
        # credit and the depth both have to move with it. On a vector entry the raster is the
        # fallback, so it is `fallback_attribution` that describes those bytes, not `attribution`.
        vendor = vendor_for(source_id)
        overridden = vendor.attribution if vendor else None
        vendor_depth = vendor.max_native_zoom if vendor else None
        if not is_vector:
            if overridden:
                entry["attribution"] = overridden
            entry["max_zoom"] = _depth_of_the_bytes(entry["max_zoom"], vendor_depth)
        if is_vector:
            entry["style_url"] = protomaps_style_url(source_id) or source["style_url"]
            # The two halves of a vector entry are different datasets (Protomaps' basemap and a
            # vendor's raster), so each carries its own credit and depth - showing one's attribution
            # over the other's bytes is a licence error, and publishing one's ceiling lets a client
            # pan past what the other can draw. Absent on a REData that predates D15.
            for field in ("fallback_attribution", "fallback_min_zoom", "fallback_max_zoom"):
                if source.get(field) is not None:
                    entry[field] = source[field]
            if overridden:
                entry["fallback_attribution"] = overridden
            raster_depth = _depth_of_the_bytes(entry.get("fallback_max_zoom"), vendor_depth)
            if raster_depth is not None:
                entry["fallback_max_zoom"] = raster_depth
        # Offered whenever REData will serve the layer tile-by-tile. A raster entry always is. A
        # vector one only since D15: before it, such a layer answered a tile request with 400, and
        # the template's absence upstream is what distinguishes the two deployments.
        if not is_vector or source.get("url_template"):
            entry["url_template"] = tile_url_template(source_id)
        layers.append(entry)
    return layers


def basemap_tile_catalogue(*, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """REData's layer catalogue, rewritten into what a browser on this deployment can actually use.

    A vendor ``url_template`` is deliberately not passed through: it needs REData's key, and handing
    the browser a template it cannot use would produce a layer that silently fails to load. What is
    published instead is this deployment's own proxy URL for that layer. A ``style_url`` needs no
    key (REData's ``D11``) and is passed through unchanged - the client fetches and renders that
    style document directly, and REData never sees a vector tile go by.

    Since REData's ``D15`` an entry can carry both, and both are kept: ``style_url`` is what a
    MapLibre map draws, ``url_template`` is what a Leaflet map draws, and a layer that publishes
    only the first leaves every Leaflet map falling back to a hardcoded vendor CDN.

    One refill at a time, deployment-wide. The cache has no jitter, so it goes cold for every
    client at the same moment, and each miss holds a request thread for the ~1.45s the call costs
    (``P131``) - the whole thread pool, on a deployment whose worker count is smaller than its
    concurrent page loads. Callers that lose the reservation wait for the winner's answer and
    degrade to the vendor layers if it does not arrive.

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

    if not single_flight.claim(CATALOGUE_FETCH_KEY, CATALOGUE_FETCH_TTL):
        return _await_catalogue()
    try:
        try:
            sources = RedataBasemapTilesGateway().list_sources()
        except (LocationContextUnavailableError, RequestCancelledError, OSError) as exc:
            # Not cached: an unreachable catalogue must not cost this
            # deployment its extra layers for a day.
            logger.warning("REData tile catalogue unavailable: %s", exc)
            return []

        layers = _offered_layers(sources)
        if layers:
            cache.set(CATALOGUE_CACHE_KEY, layers, CATALOGUE_CACHE_TTL)
        # An empty catalogue is not cached.
        return layers
    finally:
        single_flight.release(CATALOGUE_FETCH_KEY)


def catalogue_for_viewer(*, authenticated: bool, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """:func:`basemap_tile_catalogue`, filtered to what this viewer can actually fetch.

    ``url_template`` points at :class:`~urbanlens.dashboard.controllers.basemap_tiles.BasemapTileView`,
    which is login-required, so offering it to a signed-out visitor (a public share page) would swap
    a working vendor layer for a grid of 404s. A ``style_url`` is fetched straight from REData with
    no key, so it is offered to everyone - and an entry carrying both keeps only the style.

    Args:
        authenticated: Whether the viewer is signed in to this deployment.
        allow_fetch: Passed through; see :func:`basemap_tile_catalogue`.

    Returns:
        The entries this viewer can load.
    """
    layers = basemap_tile_catalogue(allow_fetch=allow_fetch)
    if authenticated:
        return layers
    # Since D15 a vector entry carries a proxy template too, so dropping the raster entries is no
    # longer enough: the template has to come off the ones that stay, or a public share page paints
    # a grid of 404s exactly where it currently paints a working layer. Copied rather than mutated,
    # because these dicts are the cached catalogue every other reader shares.
    return [{key: value for key, value in entry.items() if key != "url_template"} for entry in layers if entry.get("style_url")]
