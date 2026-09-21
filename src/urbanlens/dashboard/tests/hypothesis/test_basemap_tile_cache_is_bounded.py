"""The basemap tile proxy writes whatever the vendor sent into the shared cache.

N21 H35/H22/H13. `cache.set(cache_key, (body, resolved_type), _TILE_CACHE_TTL)`
stores raw tile bytes with no size check, into the same shared Dragonfly that holds
sessions and the Channels layer - and a full store there raises rather than
evicting to make room. One oversized tile - or a vendor answering a tile
request with something that is not a tile - can turn an unrelated cache write
into a refused one for everyone sharing the store.

`bounded_cache.set_if_small` already exists for exactly this and the Immich
thumbnail proxy already uses it, so the fix is to stop having two answers to the
same question rather than to invent a third.

It also closes an unhandled failure path that has nothing to do with size: a
bare `cache.set` against a full or unreachable Dragonfly *raises*, and here that
raise was inside the request with nothing catching it - so a cache problem
became a 500 on a map tile. `set_if_small` catches it and serves the tile
uncached, which is the right answer: refusing to cache must never mean refusing
to answer.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"
_CEILING = "urbanlens.dashboard.services.core.bounded_cache.MAX_CACHED_BODY_BYTES"


def _breaking(operation: str, reason: str = "dragonfly is full"):
    """Make one operation on the proxied-bytes store fail.

    Patching ``django.core.cache.cache`` instead reaches the store that holds the sessions, which
    the tile path stopped using - and the tile is served either way, so the test would pass
    without proving anything.
    """
    return mock.patch.object(caches[settings.PROXIED_BYTES_CACHE], operation, side_effect=ConnectionError(reason))


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
    """A full or unreachable Dragonfly is a degraded cache, not a broken map."""

    def test_a_cache_error_still_serves_the_tile(self) -> None:
        with _breaking("set"):
            responses, _download = self._fetch(b"PNGDATA")

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0].content, b"PNGDATA")

    def test_a_failing_read_still_serves_the_tile(self) -> None:
        """The read is the first cache call the view makes, so an unreachable store fails here
        before any of the write paths are even reached."""
        with _breaking("get_many"):
            responses, _download = self._fetch(b"PNGDATA")

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0].content, b"PNGDATA")

    def test_a_failing_write_still_answers_a_definitive_miss(self) -> None:
        """The 404 path writes its own sentinel, so it has its own way to 500."""
        with (
            _breaking("set"),
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(404, b"", "")),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)

    def test_a_failing_write_does_not_break_the_authorisation_note(self) -> None:
        """``remember_tile_viewer`` writes on the first tile of a session, before anything is
        cached - so a degraded store breaks every map load rather than an unlucky one."""
        from urbanlens.dashboard.services.map.tile_authorisation import remember_tile_viewer

        with _breaking("set"):
            remember_tile_viewer("some-session-key")

    def test_a_failing_delete_does_not_break_signing_out(self) -> None:
        """``forget_tile_viewer`` is wired to ``user_logged_out``, so raising here 500s the logout
        itself rather than the map."""
        from urbanlens.dashboard.services.map.tile_authorisation import forget_tile_viewer

        request = mock.Mock()
        request.session.get.return_value = "1"
        request.session.session_key = "some-session-key"
        with _breaking("delete"):
            forget_tile_viewer(sender=None, request=request)
