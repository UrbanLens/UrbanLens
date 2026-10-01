"""The sixteen tiles every map draws behind its base, and why they are kept far longer than a tile.

``frontend/ts/shared/map-layers.ts`` fills the gap a zoom opens with a blurred picture of the world
cut from the active base's own tiles. It asks for the whole world at ``z=2`` - sixteen tiles - and
then never asks for anything again, however far the viewer zooms or pans. So those sixteen are not
a tile a viewer might cross once: they are the background of every map on the site, forever, and
the only thing standing between "forever" and "sixteen fetches per layer per week" is what this
origin says about how long they may be kept.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers import basemap_tiles

_GATEWAY = "urbanlens.dashboard.services.apis.locations.basemap_vendor_tiles_gateway.BasemapVendorTilesGateway"
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

#: The depth `UNDERLAY_MOSAIC_ZOOM` names on the client. The two have to agree or the long life is
#: stamped on tiles nothing draws and withheld from the ones every map does.
_WORLD_ZOOM = 2


class UnderlayDepthTests(SimpleTestCase):
    """What counts as a world tile, decided in one place rather than at each call site."""

    def test_the_world_the_client_asks_for_is_the_world_this_origin_keeps(self) -> None:
        for z in range(_WORLD_ZOOM + 1):
            self.assertEqual(basemap_tiles._ttl_for(z), basemap_tiles._UNDERLAY_CACHE_TTL, f"z={z}")

    def test_a_tile_a_viewer_merely_crosses_is_not_kept_for_a_year(self) -> None:
        """The long life is earned by being asked for on every map forever; an ordinary tile is not."""
        for z in (_WORLD_ZOOM + 1, 12, 19):
            self.assertEqual(basemap_tiles._ttl_for(z), basemap_tiles._TILE_CACHE_TTL, f"z={z}")

    def test_an_absence_is_never_kept_as_long_as_bytes(self) -> None:
        """Bytes at this depth will not change. "No such tile" is a claim about the vendor on one
        day, and a wrong one is a quadrant of every underlay on the site missing until it expires."""
        for z in (0, _WORLD_ZOOM, 12):
            self.assertLessEqual(basemap_tiles._ttl_for_absence(z), basemap_tiles._TILE_CACHE_TTL, f"z={z}")


class UnderlayTileLifetimeTests(TestCase):
    """The same request a browser makes for one piece of the world picture."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _url(self, z: int) -> str:
        return reverse("map.basemap_tiles", kwargs={"layer": "satellite", "z": z, "x": 1, "y": 1})

    def _get(self, z: int, answer: tuple[int, bytes, str] = (200, b"PNGDATA", "image/png")):
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=answer) as download,
        ):
            first = self.client.get(self._url(z))
            second = self.client.get(self._url(z))
        return first, second, download

    def test_the_world_is_bought_once_for_the_whole_site_and_kept_for_a_year(self) -> None:
        first, second, download = self._get(_WORLD_ZOOM)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.content, b"PNGDATA")
        self.assertEqual(download.call_count, 1, "the second viewer of the world picture must not reach the vendor")
        self.assertIn(f"max-age={basemap_tiles._UNDERLAY_CACHE_TTL}", first["Cache-Control"])
        self.assertIn("immutable", first["Cache-Control"])
        self.assertIn("public", first["Cache-Control"], "a private answer is one no CDN will hold")

    def test_the_lifetime_survives_the_cache_hit(self) -> None:
        """A header stamped only on the fresh path would quietly shorten to nothing the moment the
        tile was cached - which is every request after the first, i.e. all of them."""
        _, second, _ = self._get(_WORLD_ZOOM)

        self.assertIn(f"max-age={basemap_tiles._UNDERLAY_CACHE_TTL}", second["Cache-Control"])

    def test_an_ordinary_tile_is_left_on_the_ordinary_lifetime(self) -> None:
        """This is a narrow exemption for sixteen URLs per layer, not a site-wide change to how
        long a basemap tile is held."""
        first, _, _ = self._get(12)

        self.assertIn(f"max-age={basemap_tiles._TILE_CACHE_TTL}", first["Cache-Control"])

    def test_a_missing_world_tile_is_not_a_year_long_hole(self) -> None:
        first, _, _ = self._get(_WORLD_ZOOM, answer=(404, b"", ""))

        self.assertEqual(first.status_code, 404)
        self.assertIn(f"max-age={basemap_tiles._TILE_CACHE_TTL}", first["Cache-Control"])
        self.assertNotIn(f"max-age={basemap_tiles._UNDERLAY_CACHE_TTL}", first["Cache-Control"])
