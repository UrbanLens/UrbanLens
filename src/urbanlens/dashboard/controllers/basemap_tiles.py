"""Proxy and catalogue for REData's basemap tile layers.

REData's tile endpoints require its API key, which must never reach the browser, so Leaflet points
at this view and the fetch happens server-side - the same arrangement ``historical_map_tiles`` uses
for warped overlay tiles.
Caching follows REData's own status contract rather than treating every response alike:

- ``200`` tiles are cached; a basemap tile is stable for a given z/x/y.
- ``404`` is definitive - the vendor confirmed no such tile, or the layer id is unknown - and is
  cached so a blank area does not re-ask on every pan.
- ``503`` means the tile could not be fetched - the upstream was unreachable, or this process is
  already using every upstream slot it is allowed - and is never cached. Caching it would turn a
  passing outage into a permanently blank map region.

The concurrency bound is not incidental. A viewport is ~30 tiles and the browser asks for all of
them at once, so on a cold cache the proxy can hold every request thread in the process at once,
for as long as the upstream takes per tile - measured at ~1.5s against REData in September 2026,
which is its per-request key-verification cost and not the tiles' (``P131``). Unbounded, one map
load stalls the whole site.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING

from csp.decorators import csp_exempt
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View

from urbanlens.dashboard.services.core import bounded_cache
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError, ServiceDisabledError
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Basemap tiles are stable for a given coordinate, so this is longer than the historical-map proxy's day: those
#: can change when a georeference is corrected, these change only when the vendor re-renders.
_TILE_CACHE_TTL = 7 * 86400

#: Cache sentinel for a definitive 404. Deliberately not ``b""``: a 200 whose body happens to be empty would
#: otherwise be stored as bytes identical to the sentinel and read back as "no such tile", turning a transient
#: empty answer into a permanent hole in the map.
_NO_TILE = "__ul_no_tile__"


class UpstreamSlots:
    """The process-wide bound on how many tiles may be fetched from REData at once.

    Built on first use rather than at import so a deployment (or a test) can set the cap without
    the module having already frozen it.
    """

    _semaphore: threading.BoundedSemaphore | None = None
    _lock = threading.Lock()

    @classmethod
    def semaphore(cls) -> threading.BoundedSemaphore:
        """The shared semaphore, built if this is the first call."""
        if cls._semaphore is None:
            with cls._lock:
                if cls._semaphore is None:
                    cls._semaphore = threading.BoundedSemaphore(app_settings.basemap_tile_upstream_concurrency)
        return cls._semaphore

    @classmethod
    def reset(cls) -> None:
        """Drop the semaphore so the next call rereads the setting. Test-only."""
        with cls._lock:
            cls._semaphore = None

    @classmethod
    @contextlib.contextmanager
    def hold(cls) -> Iterator[bool]:
        """Hold one slot for the block, or yield ``False`` when none is free.

        Never blocks: a request thread waiting for a slot is occupying the resource the slot
        exists to ration, so over the cap the caller answers immediately instead.
        """
        acquired = cls.semaphore().acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                cls.semaphore().release()


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


def _keep_for_a_week(response: HttpResponse) -> HttpResponse:
    """Tell the browser it may keep this answer as long as this deployment does.

    A tile is immutable for a layer and coordinate, so a re-ask gets the answer the browser already
    has. Without this the proxy is asked again for every tile on every pan back over the same
    ground, and a tile request is never free - it is an authenticated request through the whole
    middleware chain, on request threads the rest of the site is sharing.

    Args:
        response: The response to stamp.

    Returns:
        The same response.
    """
    # private: a tile is served behind a login, so a shared cache must not hold one.
    response.headers["Cache-Control"] = f"private, max-age={_TILE_CACHE_TTL}, immutable"
    return response


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
        from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
        from urbanlens.dashboard.services.map.tile_authorisation import remember_tile_viewer, session_key_for, tile_auth_key

        if not redata_configured():
            return HttpResponse(status=404)

        cache_key = f"ul_basemap_tile_{layer}_{z}_{x}_{y}"
        session_key = session_key_for(request)
        auth_key = tile_auth_key(session_key) if session_key else None
        # One round trip for both: the tile is useless without the gate and the gate costs nothing
        # to carry alongside it.
        found = cache.get_many([auth_key, cache_key] if auth_key else [cache_key])

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
                status, body, content_type = RedataBasemapTilesGateway().download_tile(layer, z, x, y)
            except (LocationContextUnavailableError, RequestCancelledError, OSError) as exc:
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
            resolved_type = content_type or "image/png"
            # Bounded like the Immich thumbnail proxy: these bytes come from a
            # vendor and land in the same shared Dragonfly that holds sessions
            # and the Channels layer - and a full store there raises rather
            # than evicting to make room, so one surprise must not turn into
            # failed cache writes for everyone sharing the store.
            # The helper also swallows a cache failure - a full or unreachable
            # Dragonfly is a degraded cache, not a broken map.
            bounded_cache.set_if_small(cache_key, body, resolved_type, _TILE_CACHE_TTL, label=f"Basemap tile {layer} {z}/{x}/{y}")
            return _keep_for_a_week(HttpResponse(body, content_type=resolved_type))
        if status in (400, 404):
            # A definitive answer about the request: no such tile, unknown
            # layer, or coordinates out of range. Safe to remember.
            cache.set(cache_key, _NO_TILE, _TILE_CACHE_TTL)
            return _keep_for_a_week(HttpResponse(status=404))
        logger.warning("Basemap tile upstream status %s for %s %s/%s/%s", status, layer, z, x, y)
        return HttpResponse(status=503)
