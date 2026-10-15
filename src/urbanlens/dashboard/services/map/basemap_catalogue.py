"""
The basemap layers this deployment can offer, from the tile catalogue a plugin provides.

Two consumers, one builder: ``BasemapTileCatalogueView`` serves this over HTTP for a client that
asks after the page has loaded, and ``{% basemap_tile_catalogue %}`` embeds it in the document so
the browser knows which tiles to draw *before* it draws any. The embed is the one that matters for
privacy: a map that paints vendor tiles first and swaps to this deployment's own afterwards has
already told that vendor which coordinates the user is looking at, which is what proxying them was
for (see ``frontend/ts/shared/map-layers.ts``'s note on ``Referrer-Policy: no-referrer``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import re
import time
from typing import Any, ClassVar
from urllib.parse import quote

from django.core.cache import cache

from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.map.basemap_vendors import vendor_for

logger = logging.getLogger(__name__)


class TileCatalogue(ABC):
    """
    A service that publishes basemap layers and serves their tiles, contributed to ``TILE_CATALOGUES``.

    Layers named in :mod:`~urbanlens.dashboard.services.map.basemap_vendors` are fetched from their vendor directly;
    this serves the rest, and publishes the list of both.

    Attributes:
        cache_tag: Fingerprint in the cache key of a tile this catalogue serves, as a vendor's endpoint fingerprint is
            in a vendor tile's, so a catalogue that changes how it produces a layer can start its tiles afresh.
    """

    cache_tag: ClassVar[str]

    @abstractmethod
    def available(self) -> bool:
        """Whether this install can ask the service."""

    @abstractmethod
    def list_sources(self) -> list[dict[str, Any]]:
        """
        The layers on offer, each with ``id``, ``name``, ``attribution``, ``source_type`` (``"raster"`` or ``"vector"``),
        ``min_zoom``, ``max_zoom`` and, for a vector layer, ``style_url``.

        Raises:
            ProviderUnavailableError: The service could not be asked.
        """

    @abstractmethod
    def download_tile(self, layer: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
        """
        Fetch one tile.

        Returns:
            ``(status_code, body, content_type)``.

        Raises:
            ProviderUnavailableError: The service could not be asked.
        """

    @abstractmethod
    def refuses_as_vector(self, status: int, body: bytes) -> bool:
        """Whether an answer to :meth:`download_tile` says the layer is a vector style the service holds no tiles for."""


def active_tile_catalogue() -> TileCatalogue | None:
    """The first available tile catalogue, or None where this install has none."""
    from urbanlens.dashboard.plugins.points import TILE_CATALOGUES

    return next((catalogue for catalogue in TILE_CATALOGUES if catalogue.available()), None)


#: The catalogue is documented as "called once per session"; a day keeps it
#: fresh enough to pick up a new layer without asking on every map load.
CATALOGUE_CACHE_TTL = 86400
#: Versioned with the entry shape, so a deploy that adds a field is not served yesterday's entries
#: for a day: v2 added ``esri_service``.
CATALOGUE_CACHE_KEY = "ul_tile_sources:v2"

#: Held for the length of one refill. Its TTL only has to outlive a gateway call that has
#: already spent the rate limiter's patience; the reservation is released on every exit.
CATALOGUE_FETCH_KEY = "ul_tile_sources:fetching"
CATALOGUE_FETCH_TTL = 30

#: How long a caller that lost the reservation waits for the winner's answer before giving up
#: and rendering vendor layers. One call costs ~1.45s.
CATALOGUE_WAIT_SECONDS = 3.0
_WAIT_POLL_SECONDS = 0.05

#: The proxy route captures ``<slug:layer>``, so this is the alphabet an id has to
#: be in to have a reachable URL at all.
_LAYER_ID = re.compile(r"[-a-zA-Z0-9_]+")


def tile_url_template(layer: str) -> str:
    """Leaflet-style template for one layer, pointing at this deployment's proxy."""
    from django.urls import reverse

    # Sentinel coordinates rather than braces: reverse() would encode braces as %7Bz%7D, handing the client a
    # template it cannot fill in. Replaced from the right, because the route's alphabet for a layer id is
    # `[-a-zA-Z0-9_]` and the id comes from the catalogue - so no numeric sentinel is one an id cannot contain, and
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
    """
    Drop the cached catalogue, so the next reader asks the service again rather than keeping a day of
    layers this deployment has since turned out to be unable to serve.

    A map draws what the catalogue named and nothing else, so a catalogue that outlives the
    deployment's ability to fetch those tiles is a grey map for as long as it is cached - where the
    vendor layers it replaced would have drawn. The proxy calls this when it is refused outright.
    """
    cache.delete(CATALOGUE_CACHE_KEY)


def _await_catalogue() -> list[dict[str, Any]]:
    """
    Wait out whoever holds the refill, rather than making a second identical call.

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
            # The holder finished without publishing, so it failed or the service offers nothing.
            break
    return []


def hosted_basemap_tiles() -> str | None:
    """
    The tile template this deployment's browsers fetch the street and dark basemaps' tiles from, when not our own.

    Protomaps' hosted API, fetched by the browser directly - the key travels in the URL, which is how a Protomaps key
    is meant to be used: its dashboard restricts the origins it answers. Only the tiles move. The style, glyphs and
    sprites stay the ones the catalogue publishes, so the map looks the same either way: both
    serve the Protomaps v4 schema. The client falls back to the style's own tiles
    when these fail (``frontend/ts/shared/hosted-basemap.ts``).

    Deliberately not part of the cached catalogue: a key rotated or removed takes effect on the next page, not a day
    later.

    Returns:
        A template with literal ``{z}``/``{x}``/``{y}`` placeholders, or None where this environment draws our own
        tiles (development, local, or no ``UL_PROTOMAPS_API_KEY``).
    """
    from urbanlens.dashboard.services.core.egress import hosted_basemap_api_key
    from urbanlens.UrbanLens.egress import PROTOMAPS_API_ORIGIN

    key = hosted_basemap_api_key()
    if not key:
        return None
    return f"{PROTOMAPS_API_ORIGIN}/tiles/v4/{{z}}/{{x}}/{{y}}.mvt?key={quote(key, safe='')}"


def _depth_of_the_bytes(published: Any, vendor_depth: int | None) -> Any:
    """
    How deep the layer goes, given who is actually being fetched.

    A layer named in the vendor table never reaches the catalogue's service, so its depth describes an endpoint
    that is not being used - too shallow and the client upscales levels the vendor would have drawn,
    too deep and the proxy fetches Esri's blank-past-coverage JPEG, which is a 200 and caches like
    any other tile. So where the vendor declares a depth it is the one to publish, either way.

    Args:
        published: The depth the catalogue published, which may be None or a non-integer.
        vendor_depth: The vendor's own deepest level, or None when it declares none.

    Returns:
        The depth to publish.
    """
    return published if vendor_depth is None else vendor_depth


def _offered_layers(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite the catalogue's sources into the entries a browser on this deployment can use."""
    layers: list[dict[str, Any]] = []
    for source in sources:
        source_id = source.get("id")
        if not isinstance(source_id, str) or not _LAYER_ID.fullmatch(source_id):
            # An id outside the route's alphabet has no proxy URL to build, and
            # reverse() answers that with NoReverseMatch rather than a skip - so
            # one such entry would otherwise 500 the whole catalogue.
            logger.warning("Skipping tile source with unusable id %r", source_id)
            continue
        if not source.get("attribution"):
            # Every vendor requires attribution on the rendered map.
            continue
        # Absent on older catalogues - treat as raster.
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
        vendor = vendor_for(source_id)
        overridden = vendor.attribution if vendor else None
        if vendor and vendor.esri_service:
            entry["esri_service"] = vendor.esri_service
        vendor_depth = vendor.max_native_zoom if vendor else None
        if not is_vector:
            if overridden:
                entry["attribution"] = overridden
            entry["max_zoom"] = _depth_of_the_bytes(entry["max_zoom"], vendor_depth)
        if is_vector:
            entry["style_url"] = source["style_url"]
            # Vector entries combine two datasets; each carries its own credit and depth.
            for field in ("fallback_attribution", "fallback_min_zoom", "fallback_max_zoom"):
                if source.get(field) is not None:
                    entry[field] = source[field]
            if overridden:
                entry["fallback_attribution"] = overridden
            raster_depth = _depth_of_the_bytes(entry.get("fallback_max_zoom"), vendor_depth)
            if raster_depth is not None:
                entry["fallback_max_zoom"] = raster_depth
        # Older catalogues gave a vector entry no template; it then returned 400.
        if not is_vector or source.get("url_template"):
            entry["url_template"] = tile_url_template(source_id)
        layers.append(entry)
    return layers


def basemap_tile_catalogue(*, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """
    The active tile catalogue's layers, rewritten into what a browser on this deployment can actually use.

    A ``url_template`` is deliberately not passed through: it needs the service's key, and handing
    the browser a template it cannot use would produce a layer that silently fails to load. What is
    published instead is this deployment's own proxy URL for that layer. A ``style_url`` needs no
    key and is passed through unchanged - the client fetches and renders that
    style document directly, and the service never sees a vector tile go by.

    An entry can carry both, and both are kept: ``style_url`` is what a
    MapLibre map draws, ``url_template`` is what a Leaflet map draws, and a layer that publishes
    only the first leaves every Leaflet map falling back to a hardcoded vendor CDN.

    One refill at a time, deployment-wide. The cache has no jitter, so it goes cold for every
    client at the same moment, and each miss holds a request thread for the ~1.45s the call costs
    - the whole thread pool, on a deployment whose worker count is smaller than its
    concurrent page loads. Callers that lose the reservation wait for the winner's answer and
    degrade to the vendor layers if it does not arrive.

    Args:
        allow_fetch: Whether a cache miss may go to the service. False for anything rendering a page:
            that call costs ~1.45s and the embed appears on every page built on
            ``themes/base.html``, so a cold cache would otherwise put a round trip inside
            the render of a profile page that has no map on it at all. A miss simply yields no
            embed, and the client's own ``registerCatalogueLayers()`` fetch - which is asynchronous,
            and which the catalogue view answers - repopulates the cache for every later render.

    Returns:
        One entry per offered layer - empty when there is no catalogue or it is unreachable, so a map
        simply keeps its built-in vendor layers.
    """
    from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError

    catalogue = active_tile_catalogue()
    if catalogue is None:
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
            sources = catalogue.list_sources()
        except (ProviderUnavailableError, RequestCancelledError, OSError) as exc:
            # Not cached: an unreachable catalogue must not cost this
            # deployment its extra layers for a day.
            logger.warning("Tile catalogue unavailable: %s", exc)
            return []

        layers = _offered_layers(sources)
        if layers:
            cache.set(CATALOGUE_CACHE_KEY, layers, CATALOGUE_CACHE_TTL)
        # An empty catalogue is not cached.
        return layers
    finally:
        single_flight.release(CATALOGUE_FETCH_KEY)


def catalogue_for_viewer(*, authenticated: bool, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """Catalogue filtered to what the viewer can fetch. Unauthenticated viewers lose url_template (login-required)."""
    layers = basemap_tile_catalogue(allow_fetch=allow_fetch)
    if authenticated:
        return layers
    # Vector entries also carry a proxy template; strip it for public viewers.
    # Copy rather than mutate: these dicts are the shared cached catalogue.
    return [{key: value for key, value in entry.items() if key != "url_template"} for entry in layers if entry.get("style_url")]
