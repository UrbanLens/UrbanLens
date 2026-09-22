"""Proxy and catalogue for the basemap tile layers REData publishes.

The fetch happens server-side for two different reasons, and only one of them is about a key.
REData's own endpoints require its API key, which must never reach the browser. The raster layers
it publishes need no key at all - they are public vendor endpoints - and are proxied for privacy:
a map page URL can encode a pin's coordinates, so which tiles a viewer asks for is which places
they are looking at, and this site's blanket ``Referrer-Policy: no-referrer`` is what free vendor
CDNs answer with 403 when a browser asks them directly. ``services/map/basemap_vendors.py`` decides
which of the two a given layer takes; ``historical_map_tiles`` uses the same arrangement for warped
overlay tiles.

Caching follows the upstream's status contract rather than treating every response alike:

- ``200`` tiles are cached; a basemap tile is stable for a given z/x/y.
- ``404`` is definitive - the vendor confirmed no such tile, or the layer id is unknown - and is
  cached so a blank area does not re-ask on every pan.
- ``503`` means the tile could not be fetched - the upstream was unreachable, or this process is
  already using every upstream slot it is allowed - and is never cached. Caching it would turn a
  passing outage into a permanently blank map region.

The concurrency bound is not incidental. A viewport is ~30 tiles and the browser asks for all of
them at once, so on a cold cache the proxy can hold every request thread in the process at once,
for as long as the upstream takes per tile. Unbounded, one map load stalls the whole site. What
that costs now depends on which upstream the layer takes: measured on ``k3s-staging`` in September
2026, Esri answers a satellite tile in 0.09s where REData took 0.36-0.56s for the same tile.
"""

from __future__ import annotations

import logging

from csp.decorators import csp_exempt
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View

from urbanlens.dashboard.middleware import mark_shared_cacheable
from urbanlens.dashboard.services.core import bounded_cache
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, servable_tile_type
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError, ServiceDisabledError
from urbanlens.dashboard.services.core.upstream_slots import UpstreamSlots as BaseUpstreamSlots
from urbanlens.UrbanLens.settings.app import settings as app_settings

logger = logging.getLogger(__name__)

#: Basemap tiles are stable for a given coordinate, so this is longer than the historical-map proxy's day: those
#: can change when a georeference is corrected, these change only when the vendor re-renders.
_TILE_CACHE_TTL = 7 * 86400

#: Cache sentinel for a definitive 404. Deliberately not ``b""``: a 200 whose body happens to be empty would
#: otherwise be stored as bytes identical to the sentinel and read back as "no such tile", turning a transient
#: empty answer into a permanent hole in the map.
_NO_TILE = "__ul_no_tile__"

#: REData's 400 for a layer it publishes as a vector style rather than as tiles (its ``D11``). An
#: answer about the layer, not the coordinate, so it is neither cacheable per tile nor something the
#: catalogue that advertised the layer as raster should outlive.
#: Live, not defensive: ``redata.urbanlens.org`` answers ``street`` and ``dark`` this way today,
#: whatever REData's ``D15`` and ``T9`` say about every layer carrying both shapes. Without this
#: branch such an answer falls through to the definitive-404 one and holds a week-long hole.
_VECTOR_LAYER_REFUSAL = b"vector_layer_not_served"


class UpstreamSlots(BaseUpstreamSlots):
    """The process-wide bound on how many basemap tiles may be fetched upstream at once."""

    @classmethod
    def limit(cls) -> int:
        """How many basemap tile fetches one process may have in flight.

        Returns:
            ``basemap_tile_upstream_concurrency``.
        """
        return app_settings.basemap_tile_upstream_concurrency


class BasemapTileCatalogueView(LoginRequiredMixin, View):
    """GET map/basemap-tiles/sources/ - the layers this deployment can offer.

    A fallback path, not the main one: every page built on ``themes/base.html`` already carries this
    same catalogue inline (``{% basemap_tile_catalogue %}``), because a map has to know which tiles
    to draw before it draws any. This answers a client that was rendered without it.
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return the layer catalogue for the signed-in viewer.

        Args:
            request: The current request.

        Returns:
            ``{"layers": [...]}`` - empty when REData is unconfigured or unreachable, so the map simply
            keeps its built-in layers.
        """
        from urbanlens.dashboard.services.map.basemap_catalogue import catalogue_for_viewer

        return JsonResponse({"layers": catalogue_for_viewer(authenticated=request.user.is_authenticated)})


def _fetch_tile(layer: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
    """Fetch one uncached tile, from the vendor where that is possible.

    REData needs its API key and so cannot be called from a browser, but the raster layers it
    publishes are keyless vendor endpoints it is itself fetching - so going through it is a second
    round trip that buys nothing. Measured on staging, it is most of the cost: 0.36-0.56s for a
    satellite tile Esri answers in 0.09s.

    Args:
        layer: Layer id from the catalogue.
        z: Tile zoom level.
        x: Tile column.
        y: Tile row.

    Returns:
        ``(status_code, body, content_type)``.
    """
    from urbanlens.dashboard.services.apis.locations.basemap_vendor_tiles_gateway import BasemapVendorTilesGateway
    from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway
    from urbanlens.dashboard.services.map.basemap_vendors import vendor_for

    if vendor_for(layer) is not None:
        return BasemapVendorTilesGateway().download_tile(layer, z, x, y)
    return RedataBasemapTilesGateway().download_tile(layer, z, x, y)


def _keep_for_a_week(response: HttpResponse) -> HttpResponse:
    """Tell the browser it may keep this answer as long as this deployment does, and not guess at it.

    A tile is immutable for a layer and coordinate, so a re-ask gets the answer the browser already
    has. Without this the proxy is asked again for every tile on every pan back over the same
    ground, and a tile request is never free - it is an authenticated request through the whole
    middleware chain, on request threads the rest of the site is sharing.

    Args:
        response: The response to stamp.

    Returns:
        The same response.
    """
    # public, though the endpoint is behind a login: the gate is on who may spend this deployment's
    # upstream quota, not on the bytes, which are the vendor's own basemap and the same for every
    # viewer. `private` here bought nothing and cost everything - a CDN refuses to store it, so
    # every tile of every viewport was answered by a request thread (see `mark_shared_cacheable`
    # for why saying `public` is only half of it).
    response.headers["Cache-Control"] = f"public, max-age={_TILE_CACHE_TTL}, immutable"
    # The type the upstream declared is allow-listed before it gets here; this is the other half,
    # for bytes that do not match the type they were allowed under. nginx sets it on the media
    # routes only, and this one is csp_exempt.
    response.headers["X-Content-Type-Options"] = "nosniff"
    return mark_shared_cacheable(response)


# A Content-Security-Policy governs what a *document* may load, so on a tile it is ~1.2kB of header
# that can never apply - paid ~30 times per map opened. Both spellings, because the site emits the
# report-only header until `UL_CSP_ENFORCE` flips it to the enforcing one.
@method_decorator(csp_exempt(REPORT_ONLY=True), name="dispatch")
@method_decorator(csp_exempt(REPORT_ONLY=False), name="dispatch")
class BasemapTileView(AccessMixin, View):
    """GET map/basemap-tiles/<layer>/<z>/<x>/<y>/ - one basemap tile.

    Not ``LoginRequiredMixin``: the gate is the same, but it is answered from the cache the tile
    itself comes out of, so a viewport does not spend a query per tile re-establishing that the
    same person is still signed in. See ``services/map/tile_authorisation.py`` for what that
    trades away; the catalogue view above keeps the ordinary check.
    """

    def get(self, request: HttpRequest, layer: str, z: int, x: int, y: int) -> HttpResponse:
        """Serve one tile from cache or REData.

        Args:
            request: The current request.
            layer: Layer id from the catalogue.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            The tile bytes, a definitive 404, an uncached 503 when the vendor could not be reached,
            or whatever a login-required view answers a signed-out visitor with.
        """
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
        from urbanlens.dashboard.services.map.tile_authorisation import remember_tile_viewer, session_key_for, tile_auth_key
        from urbanlens.dashboard.services.map.tile_cache_keys import basemap_tile_cache_key

        if not redata_configured():
            return HttpResponse(status=404)

        cache_key = basemap_tile_cache_key(layer, z, x, y)
        session_key = session_key_for(request)
        auth_key = tile_auth_key(session_key) if session_key else None
        # One round trip for both: the tile is useless without the gate and the gate costs nothing
        # to carry alongside it.
        found = bounded_cache.get_many_or_empty([auth_key, cache_key] if auth_key else [cache_key], label=f"Basemap tile {layer} {z}/{x}/{y}")

        if auth_key is None or auth_key not in found:
            # Nothing remembered about this session, so ask properly - and remember the answer.
            if not request.user.is_authenticated:
                return self.handle_no_permission()
            if session_key:
                remember_tile_viewer(session_key)

        cached = found.get(cache_key)
        if cached is not None:
            if cached == _NO_TILE:
                return _keep_for_a_week(HttpResponse(status=404))
            # The vendor's own content type is cached with the bytes: these layers are not all PNG, and
            # mislabelling a JPEG or WebP on the cache-hit path but not the fresh one is the kind of difference
            # that shows up only once a layer is already in the cache.
            body, content_type = cached
            return _keep_for_a_week(HttpResponse(body, content_type=content_type))

        with UpstreamSlots.hold() as slot:
            if not slot:
                # Uncached, like every other 503 here: the tile is fine, this process is just
                # already fetching as many as it is allowed to at once. Retry-After says so - the
                # client paces its requests to this budget and retries what it is still refused
                # (`frontend/ts/shared/own-tiles.ts`) rather than leaving a hole in the map, since a
                # viewport-sized burst asks for far more tiles at once than there are slots.
                return HttpResponse(status=503, headers={"Retry-After": "1"})
            try:
                status, body, content_type = _fetch_tile(layer, z, x, y)
            except (LocationContextUnavailableError, RequestCancelledError, GatewayRequestError, OSError) as exc:
                logger.warning("Basemap tile fetch failed for %s %s/%s/%s: %s", layer, z, x, y, exc)
                if isinstance(exc, ServiceDisabledError):
                    # Switched off rather than busy or unreachable, so it will still be switched off
                    # for as long as the catalogue advertising these layers stays cached - and a map
                    # draws what the catalogue named and nothing else. A rate limit deliberately
                    # does not land here: being over budget for a minute is not worth trading the
                    # layers, or the REData call that rebuilding the catalogue costs.
                    from urbanlens.dashboard.services.map.basemap_catalogue import forget_basemap_tile_catalogue

                    forget_basemap_tile_catalogue()
                return HttpResponse(status=503)

        if status == 200:
            resolved_type = servable_tile_type(content_type)
            if resolved_type is None:
                # Not cached, and not a retryable status: the upstream will answer the next
                # coordinate the same way, and a viewport retrying five times each would turn one
                # broken layer into 150 calls. Uncached so a fixed upstream is visible at once.
                logger.warning("REData answered %s %s/%s/%s with %r, which this origin will not serve", layer, z, x, y, content_type)
                return HttpResponse(status=404)
            # Bounded like the Immich thumbnail proxy: these bytes come from a
            # vendor and land in the same shared Dragonfly that holds sessions
            # and the Channels layer - and a full store there raises rather
            # than evicting to make room, so one surprise must not turn into
            # failed cache writes for everyone sharing the store.
            # The helper also swallows a cache failure - a full or unreachable
            # Dragonfly is a degraded cache, not a broken map.
            bounded_cache.set_if_small(cache_key, body, resolved_type, _TILE_CACHE_TTL, label=f"Basemap tile {layer} {z}/{x}/{y}")
            return _keep_for_a_week(HttpResponse(body, content_type=resolved_type))
        if status == 400 and _VECTOR_LAYER_REFUSAL in body:
            # Same reasoning as the disabled-service branch above: the catalogue is what named this
            # layer as raster, so it is the stale thing. Remembering the refusal per coordinate
            # instead would keep answering 404 for a week after the layer is servable again.
            from urbanlens.dashboard.services.map.basemap_catalogue import forget_basemap_tile_catalogue

            logger.warning("REData now publishes %s as a vector layer; dropping the cached tile catalogue", layer)
            forget_basemap_tile_catalogue()
            return HttpResponse(status=404)
        if status in (400, 404):
            # A definitive answer about the request: no such tile, unknown
            # layer, or coordinates out of range. Safe to remember.
            bounded_cache.set_or_skip(cache_key, _NO_TILE, _TILE_CACHE_TTL, label=f"Basemap tile {layer} {z}/{x}/{y} (absent)")
            return _keep_for_a_week(HttpResponse(status=404))
        logger.warning("Basemap tile upstream status %s for %s %s/%s/%s", status, layer, z, x, y)
        return HttpResponse(status=503)


#: What a vector tile may be served as. Kept apart from ``SERVABLE_TILE_TYPES``, which is images
#: only - that set being images is a property the raster path relies on, not an oversight to widen.
_SERVABLE_VECTOR_TYPES = frozenset({"application/x-protobuf", "application/vnd.mapbox-vector-tile"})


def _servable_vector_type(content_type: str | None) -> str | None:
    """The type a proxied vector tile may be served as.

    Args:
        content_type: What the upstream declared, header parameters and all.

    Returns:
        The type to serve it as, or None when it is not something this origin should hand a browser.
    """
    declared = (content_type or "").partition(";")[0].strip().lower()
    if not declared:
        return "application/x-protobuf"
    return declared if declared in _SERVABLE_VECTOR_TYPES else None


def _origin_of(request: HttpRequest) -> str:
    """The ``Origin`` to present upstream, which is this deployment's own.

    Protomaps refuses a request carrying none. Built from the request rather than a setting so a
    deployment reachable on more than one host presents the one actually in use.

    Args:
        request: The current request.

    Returns:
        A scheme-and-host origin with no trailing slash.
    """
    return request.build_absolute_uri("/").rstrip("/")


@method_decorator(csp_exempt(REPORT_ONLY=True), name="dispatch")
@method_decorator(csp_exempt(REPORT_ONLY=False), name="dispatch")
class VectorBasemapTileView(AccessMixin, View):
    """GET map/basemap-vector/tiles/<z>/<x>/<y>/ - one Protomaps vector tile, from this origin.

    Not ``LoginRequiredMixin``, for the reason ``BasemapTileView`` is not: the gate is answered from
    the cache the tile itself comes out of, so a viewport does not spend a query per tile.
    """

    def get(self, request: HttpRequest, z: int, x: int, y: int) -> HttpResponse:
        """Serve one vector tile from cache, or buy it once on everyone's behalf.

        Args:
            request: The current request.
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.

        Returns:
            The tile bytes, a 404 when this deployment buys no hosted basemap, or an uncached 503
            when the upstream could not be reached.
        """
        from urbanlens.dashboard.services.apis.locations.protomaps_basemap_gateway import ProtomapsBasemapGateway
        from urbanlens.dashboard.services.map.tile_authorisation import remember_tile_viewer, session_key_for, tile_auth_key
        from urbanlens.dashboard.services.map.tile_cache_keys import vector_tile_cache_key

        key = app_settings.protomaps_api_key
        if not key:
            return HttpResponse(status=404)

        cache_key = vector_tile_cache_key(z, x, y)
        session_key = session_key_for(request)
        auth_key = tile_auth_key(session_key) if session_key else None
        found = bounded_cache.get_many_or_empty([auth_key, cache_key] if auth_key else [cache_key], label=f"Vector tile {z}/{x}/{y}")

        if auth_key is None or auth_key not in found:
            if not request.user.is_authenticated:
                return self.handle_no_permission()
            if session_key:
                remember_tile_viewer(session_key)

        cached = found.get(cache_key)
        if cached is not None:
            if cached == _NO_TILE:
                return _keep_for_a_week(HttpResponse(status=404))
            body, content_type = cached
            return _keep_for_a_week(HttpResponse(body, content_type=content_type))

        with UpstreamSlots.hold() as slot:
            if not slot:
                return HttpResponse(status=503, headers={"Retry-After": "1"})
            try:
                status, body, content_type = ProtomapsBasemapGateway().download_tile(z, x, y, key=key, origin=_origin_of(request))
            except (RequestCancelledError, GatewayRequestError, OSError) as exc:
                logger.warning("Vector basemap tile fetch failed for %s/%s/%s: %s", z, x, y, exc)
                return HttpResponse(status=503)

        if status == 200:
            resolved_type = _servable_vector_type(content_type)
            if resolved_type is None:
                logger.warning("Protomaps answered %s/%s/%s with %r, which this origin will not serve", z, x, y, content_type)
                return HttpResponse(status=404)
            bounded_cache.set_if_small(cache_key, body, resolved_type, _TILE_CACHE_TTL, label=f"Vector tile {z}/{x}/{y}")
            return _keep_for_a_week(HttpResponse(body, content_type=resolved_type))
        if status in (400, 404):
            # Most of the pyramid past the source's own maxzoom is empty; re-asking on every pan is
            # what that costs, and here it costs quota rather than only a round trip.
            bounded_cache.set_or_skip(cache_key, _NO_TILE, _TILE_CACHE_TTL, label=f"Vector tile {z}/{x}/{y} (absent)")
            return _keep_for_a_week(HttpResponse(status=404))
        logger.warning("Protomaps vector tile upstream status %s for %s/%s/%s", status, z, x, y)
        return HttpResponse(status=503)


class VectorBasemapStyleView(LoginRequiredMixin, View):
    """GET map/basemap-vector/<theme>/style/ - the hosted style, with its tiles pointed back here.

    Rewriting the ``tiles`` array is the whole of the proxy as far as the browser is concerned: a
    style still naming ``api.protomaps.com`` is one MapLibre reads and then fetches every tile
    from, cache or no cache. Serving it from here also gives a 65kB document upstream sends with no
    cache headers a lifetime, and keeps the key out of a document every viewer can read.
    """

    def get(self, request: HttpRequest, theme: str) -> HttpResponse:
        """Serve one rewritten style document.

        Args:
            request: The current request.
            theme: The Protomaps theme name.

        Returns:
            The style JSON, or 404 when this deployment buys no hosted basemap or does not offer
            this theme.
        """
        import json

        from urbanlens.dashboard.services.apis.locations.protomaps_basemap_gateway import SERVED_THEMES, ProtomapsBasemapGateway
        from urbanlens.dashboard.services.map.basemap_catalogue import vector_tile_url_template
        from urbanlens.dashboard.services.map.tile_cache_keys import vector_style_cache_key

        key = app_settings.protomaps_api_key
        if not key or theme not in SERVED_THEMES:
            return HttpResponse(status=404)

        origin = _origin_of(request)
        cache_key = vector_style_cache_key(theme, origin)
        cached = bounded_cache.get_many_or_empty([cache_key], label=f"Vector style {theme}").get(cache_key)
        if cached is not None:
            body, content_type = cached
            return _keep_for_a_week(HttpResponse(body, content_type=content_type))

        with UpstreamSlots.hold() as slot:
            if not slot:
                return HttpResponse(status=503, headers={"Retry-After": "1"})
            try:
                status, raw, _ = ProtomapsBasemapGateway().download_style(theme, key=key, origin=origin)
            except (RequestCancelledError, GatewayRequestError, OSError) as exc:
                logger.warning("Vector basemap style fetch failed for %s: %s", theme, exc)
                return HttpResponse(status=503)

        if status != 200:
            logger.warning("Protomaps vector style upstream status %s for %s", status, theme)
            return HttpResponse(status=503)
        try:
            style = json.loads(raw)
        except ValueError:
            logger.warning("Protomaps answered the %s style with something that is not JSON", theme)
            return HttpResponse(status=503)
        if not isinstance(style, dict):
            return HttpResponse(status=503)

        # Absolute, rather than the path `reverse()` gives: the client resolves a style's relative
        # URLs against the style's own address, and the address it has for this document is itself
        # a path - so a relative tile template would be left for MapLibre to interpret. Built by
        # concatenation because `build_absolute_uri` percent-encodes the `{z}` tokens.
        template = f"{origin}{vector_tile_url_template()}"
        for source in style.get("sources", {}).values():
            if isinstance(source, dict) and source.get("tiles"):
                # Only the tile endpoint moves. `glyphs` and `sprite` are GitHub Pages rather than
                # the metered API, and repointing them at a path this origin does not serve would
                # cost the map its labels.
                source["tiles"] = [template]
                source.pop("url", None)

        body = json.dumps(style).encode()
        bounded_cache.set_if_small(cache_key, body, "application/json", _TILE_CACHE_TTL, label=f"Vector style {theme}")
        return _keep_for_a_week(HttpResponse(body, content_type="application/json"))
