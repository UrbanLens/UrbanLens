"""The metered vector basemap, fetched by this origin rather than by every browser.

Protomaps' CDN serves a tile with ``cache-control: public, max-age=14400`` and an ``age`` that
routinely exceeds it - measured against ``k3s-staging`` on 2026-09-22, every tile of a viewport
arrived ``age=52283`` against that 4-hour lifetime, so it was stale before it was drawn. A browser
cannot reuse a stale response without revalidating, and a revalidation is another billed request,
so the browser cache never takes a single tile off the meter no matter how long the viewer stays.

Nothing here can change the upstream's headers. What it can do is stop pointing browsers at the
meter: fetch once, keep the bytes in the cache this deployment already runs for raster tiles, and
serve them from this origin under a lifetime that is actually usable. The quota then follows how
many distinct tiles the whole site has ever drawn in a week, not how many times each viewer drew
them.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponse
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.map.basemap_catalogue import _offered_layers
from urbanlens.UrbanLens.settings.app import settings as app_settings

_GATEWAY = "urbanlens.dashboard.services.apis.locations.protomaps_basemap_gateway.ProtomapsBasemapGateway"

#: What Protomaps' hosted style actually contains, trimmed to the parts the rewrite touches.
_UPSTREAM_STYLE = {
    "version": 8,
    "name": "Protomaps light",
    "glyphs": "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
    "sprite": "https://protomaps.github.io/basemaps-assets/sprites/v4/light",
    "sources": {
        "protomaps": {
            "type": "vector",
            "attribution": "Protomaps © OpenStreetMap",
            "tiles": ["https://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt?key=SECRET"],
            "maxzoom": 15,
        }
    },
    "layers": [{"id": "background", "type": "background"}],
}


class VectorProxyTestCase(TestCase):
    """A signed-in viewer on a deployment configured to buy the hosted basemap."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self._original_key = app_settings.protomaps_api_key
        self.addCleanup(setattr, app_settings, "protomaps_api_key", self._original_key)
        app_settings.protomaps_api_key = "SECRET"


class VectorTileProxyTests(VectorProxyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("map.basemap_vector_tiles", kwargs={"z": 12, "x": 1206, "y": 1524})

    def test_a_tile_is_served_and_fetched_only_once(self) -> None:
        """The whole point: the second viewer of a tile costs nothing, where today each viewer pays."""
        with mock.patch(
            f"{_GATEWAY}.download_tile", return_value=(200, b"MVTBYTES", "application/x-protobuf")
        ) as download:
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"MVTBYTES")
        self.assertEqual(second.content, b"MVTBYTES")
        self.assertEqual(first["Content-Type"], "application/x-protobuf")
        self.assertEqual(download.call_count, 1, "a tile already fetched must not reach the meter again")

    def test_the_browser_is_given_a_lifetime_it_can_actually_use(self) -> None:
        """Upstream says ``max-age=14400`` and then hands back responses older than that. This
        origin serves its own bytes, so it can state a lifetime that is true when it is read."""
        with mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"MVTBYTES", "application/x-protobuf")):
            response = self.client.get(self.url)

        self.assertIn("immutable", response["Cache-Control"])
        self.assertIn("public", response["Cache-Control"])
        self.assertIn("max-age=604800", response["Cache-Control"])

    def test_a_signed_out_visitor_is_refused(self) -> None:
        """The gate is on who may spend this deployment's quota, exactly as for the raster proxy."""
        self.client.logout()

        with mock.patch(
            f"{_GATEWAY}.download_tile", return_value=(200, b"MVTBYTES", "application/x-protobuf")
        ) as download:
            response = self.client.get(self.url)

        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(download.call_count, 0)

    def test_bytes_the_upstream_does_not_call_a_vector_tile_are_not_served(self) -> None:
        """An HTML error page served back as a tile is how a broken upstream becomes a stored
        week-long lie; the raster proxy refuses the same way."""
        with mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"<html>rate limited</html>", "text/html")):
            response = self.client.get(self.url)

        self.assertNotEqual(response.status_code, 200)

    def test_an_unconfigured_deployment_has_nothing_to_proxy(self) -> None:
        """Without a key this deployment draws its own archive, and this route must not invent a
        Protomaps request it was never configured to make."""
        app_settings.protomaps_api_key = ""

        with mock.patch(f"{_GATEWAY}.download_tile") as download:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(download.call_count, 0)


class VectorStyleProxyTests(VectorProxyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("map.basemap_vector_style", kwargs={"theme": "light"})

    def _response(self, **extra: str) -> HttpResponse:
        with mock.patch(
            f"{_GATEWAY}.download_style", return_value=(200, json.dumps(_UPSTREAM_STYLE).encode(), "application/json")
        ):
            response = self.client.get(self.url, **extra)
        self.assertEqual(response.status_code, 200)
        return response

    def _serve(self) -> dict:
        return json.loads(self._response().content)

    def test_the_style_points_its_tiles_back_at_this_origin(self) -> None:
        """A style whose ``tiles`` still name the meter proxies nothing - MapLibre reads that array
        and goes straight there, and every tile is billed exactly as before."""
        style = self._serve()

        tiles = style["sources"]["protomaps"]["tiles"]
        self.assertEqual(len(tiles), 1)
        self.assertNotIn("api.protomaps.com", tiles[0])
        self.assertIn("/basemap-vector/tiles/", tiles[0])
        self.assertTrue(
            tiles[0].startswith("http://"),
            "the client resolves a style's relative URLs against an address that is itself relative here",
        )
        self.assertIn("{z}", tiles[0], "the z/x/y tokens must survive URL building or MapLibre asks for a literal path")
        self.assertIn("{x}", tiles[0])
        self.assertIn("{y}", tiles[0])

    @override_settings(ALLOWED_HOSTS=["alpha.example.com", "beta.example.com", "testserver"])
    def test_one_hosts_document_is_not_served_to_another(self) -> None:
        """The tile endpoint is named absolutely, so a cache keyed on the theme alone would send
        viewers of one hostname to the other."""
        first = json.loads(self._response(HTTP_HOST="alpha.example.com").content)["sources"]["protomaps"]["tiles"][0]
        second = json.loads(self._response(HTTP_HOST="beta.example.com").content)["sources"]["protomaps"]["tiles"][0]

        self.assertIn("alpha.example.com", first)
        self.assertIn("beta.example.com", second)

    def test_the_key_never_reaches_the_browser(self) -> None:
        """It is a quota, and it is accepted from any Origin - see the gateway's own note - so a
        copy in a public document is a copy anyone can spend."""
        style = self._serve()

        self.assertNotIn("SECRET", json.dumps(style))

    def test_the_document_the_style_depends_on_is_left_alone(self) -> None:
        """Glyphs and sprites are GitHub Pages, not the metered API; rewriting them would point the
        map at an origin this deployment does not serve."""
        style = self._serve()

        self.assertEqual(style["glyphs"], _UPSTREAM_STYLE["glyphs"])
        self.assertEqual(style["sprite"], _UPSTREAM_STYLE["sprite"])
        self.assertEqual(style["layers"], _UPSTREAM_STYLE["layers"])

    def test_the_style_is_fetched_once_and_then_cached(self) -> None:
        """Upstream sends this 65kB document with no cache headers at all, so every map page on the
        site re-fetches it."""
        with mock.patch(
            f"{_GATEWAY}.download_style", return_value=(200, json.dumps(_UPSTREAM_STYLE).encode(), "application/json")
        ) as download:
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(download.call_count, 1)

    def test_a_theme_protomaps_does_not_publish_is_refused(self) -> None:
        with mock.patch(f"{_GATEWAY}.download_style") as download:
            response = self.client.get(reverse("map.basemap_vector_style", kwargs={"theme": "terrain"}))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(download.call_count, 0)


class CatalogueAdvertisesTheProxyTests(VectorProxyTestCase):
    """Where the swap has to happen: the catalogue is what every map reads to decide what to fetch."""

    def _entry(self) -> dict:
        sources = [
            {
                "id": "street",
                "name": "Street",
                "source_type": "vector",
                "attribution": "Protomaps © OpenStreetMap",
                "style_url": "https://tiles.urbanlens.org/styles/street.json",
                "url_template": "https://tiles.urbanlens.org/street/{z}/{x}/{y}.png",
                "min_zoom": 0,
                "max_zoom": 15,
            }
        ]
        entries = _offered_layers(sources)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_the_advertised_style_is_this_origins_and_carries_no_key(self) -> None:
        entry = self._entry()

        self.assertNotIn("api.protomaps.com", entry["style_url"])
        self.assertNotIn("SECRET", entry["style_url"])
        self.assertIn("/basemap-vector/", entry["style_url"])

    def test_an_unconfigured_deployment_still_keeps_what_redata_published(self) -> None:
        """The self-hosted path is the default and this must not capture it."""
        app_settings.protomaps_api_key = ""

        self.assertEqual(self._entry()["style_url"], "https://tiles.urbanlens.org/styles/street.json")
