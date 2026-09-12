"""The basemap tile proxy writes whatever the vendor sent into the shared cache.

N21 H35/H22/H13. `cache.set(cache_key, (body, resolved_type), _TILE_CACHE_TTL)`
stores raw tile bytes with no size check, into the same 512MB Valkey that holds
sessions, the Channels layer and the Celery broker. One oversized tile - or a
vendor answering a tile request with something that is not a tile - evicts other
people's sessions to make room for itself.

`bounded_cache.set_if_small` already exists for exactly this and the Immich
thumbnail proxy already uses it, so the fix is to stop having two answers to the
same question rather than to invent a third.

It also closes an unhandled failure path that has nothing to do with size: a
bare `cache.set` against a full or unreachable Valkey *raises*, and here that
raise was inside the request with nothing catching it - so a cache problem
became a 500 on a map tile. `set_if_small` catches it and serves the tile
uncached, which is the right answer: refusing to cache must never mean refusing
to answer.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"
_CEILING = "urbanlens.dashboard.services.core.bounded_cache.MAX_CACHED_BODY_BYTES"


class _TileCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})

    def _fetch(self, body: bytes, times: int = 1):
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, body, "image/png")) as download,
        ):
            responses = [self.client.get(self.url) for _ in range(times)]
        return responses, download


class TheTileCacheIsBoundedTests(_TileCase):
    """One oversized tile must not get to evict everything else in the instance."""

    def test_an_oversized_tile_is_not_cached(self) -> None:
        with mock.patch(_CEILING, 16):
            responses, download = self._fetch(b"x" * 4096, times=2)

        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(download.call_count, 2, "the oversized tile was cached and served from the cache")

    def test_an_oversized_tile_is_still_served(self) -> None:
        """Refusing to cache must never mean refusing to answer."""
        with mock.patch(_CEILING, 16):
            responses, _download = self._fetch(b"x" * 4096)

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0].content, b"x" * 4096)


class OrdinaryTilesStillCacheTests(_TileCase):
    """The half that stops the ceiling passing against a proxy that caches nothing."""

    def test_an_ordinary_tile_is_cached(self) -> None:
        responses, download = self._fetch(b"PNGDATA", times=2)

        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(download.call_count, 1, "the second request should have been a cache hit")


class ACacheFailureIsNotA500Tests(_TileCase):
    """A full or unreachable Valkey is a degraded cache, not a broken map."""

    def test_a_cache_error_still_serves_the_tile(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.core.bounded_cache.cache.set", side_effect=ConnectionError("valkey is full")
        ):
            responses, _download = self._fetch(b"PNGDATA")

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0].content, b"PNGDATA")
