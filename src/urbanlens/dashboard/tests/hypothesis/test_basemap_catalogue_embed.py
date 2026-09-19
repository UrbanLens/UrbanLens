"""The basemap catalogue embedded in the page, ahead of any map.

A map that fetches the catalogue over HTTP has already drawn a vendor's tiles by the time the
answer lands, which hands that vendor the coordinates the proxy exists to keep from it. These
cover the embed that closes that window - and that it is actually in the base template, since a
template tag nothing renders is indistinguishable from one that does not exist.
"""

from __future__ import annotations

import json
import re
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.core.cache import cache
from django.template import Context, Template
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

_RASTER = {
    "id": "street",
    "name": "Street",
    "source_type": "raster",
    "attribution": "Esri, HERE, Garmin",
    "min_zoom": 0,
    "max_zoom": 19,
    "url_template": "https://redata.example/api/v1/tiles/street/{z}/{x}/{y}/",
}
_VECTOR = {
    "id": "terrain",
    "name": "Terrain",
    "source_type": "vector",
    "attribution": "© OpenStreetMap contributors",
    "min_zoom": 0,
    "max_zoom": 12,
    "style_url": "https://tiles.example/terrain/style.json",
}


def _warm(sources: list[dict[str, object]]) -> None:
    """Populate the catalogue cache the way a client's own fetch does.

    The embed never fetches - see ``basemap_tile_catalogue``'s ``allow_fetch`` - so a test that
    only mocks the gateway and renders would be testing the cold path every time.
    """
    from urbanlens.dashboard.services.map.basemap_catalogue import basemap_tile_catalogue

    with mock.patch(_CONFIGURED, return_value=True), mock.patch(f"{_GATEWAY}.list_sources", return_value=sources):
        basemap_tile_catalogue()


def _render(user: User | AnonymousUser) -> str:
    request = RequestFactory().get("/")
    request.user = user
    return Template("{% load map_components %}{% basemap_tile_catalogue %}").render(Context({"request": request}))


def _embedded(html: str) -> list[dict[str, object]]:
    match = re.search(r'<script id="ul-basemap-tiles" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert match, f"no embedded catalogue in {html!r}"
    return json.loads(match.group(1))


class BasemapCatalogueEmbedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)

    def test_a_signed_in_viewer_gets_the_whole_catalogue(self) -> None:
        _warm([_RASTER, _VECTOR])

        layers = _embedded(_render(self.user))

        self.assertEqual([entry["id"] for entry in layers], ["street", "terrain"])
        self.assertEqual(layers[0]["url_template"], "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/")
        self.assertEqual(layers[1]["style_url"], "https://tiles.example/terrain/style.json")

    def test_a_signed_out_viewer_is_offered_no_raster_layer(self) -> None:
        """The raster proxy is login-required, so offering one here would swap a working vendor layer for a grid of 404s on exactly the pages a signed-out visitor sees - a public share."""
        _warm([_RASTER, _VECTOR])

        layers = _embedded(_render(AnonymousUser()))

        self.assertEqual(
            [entry["id"] for entry in layers], ["terrain"], "only the keyless vector entry is fetchable signed out"
        )

    def test_nothing_is_emitted_when_this_deployment_offers_no_layers(self) -> None:
        """A self-hosted install with no REData must get no element at all, not an empty one: absent is what tells the client to ask over HTTP instead, and an empty array is an answer."""
        with mock.patch(_CONFIGURED, return_value=False):
            self.assertEqual(_render(self.user).strip(), "")

    def test_a_hostile_attribution_cannot_break_out_of_the_script_block(self) -> None:
        """Attribution is whatever REData's upstream vendor publishes - it reaches this template unreviewed."""
        _warm([dict(_RASTER, attribution="</script><img src=x onerror=alert(1)>")])

        html = _render(self.user)

        self.assertNotIn("</script><img", html)
        self.assertEqual(
            _embedded(html)[0]["attribution"],
            "</script><img src=x onerror=alert(1)>",
            "the value must survive intact once parsed",
        )

    def test_the_embed_precedes_core_js_in_the_base_template(self) -> None:
        """core.js installs window.MapLayers, and map-layers.ts reads this element the first time it is asked for a tile source. Emitted after that script, it would still be parsed in time today - but the ordering is the invariant that keeps it so, not a coincidence to rely on."""
        _warm([_RASTER])

        with mock.patch(_CONFIGURED, return_value=True):
            self.client.force_login(self.user)
            response = self.client.get("/dashboard/map/")

        html = response.content.decode()
        embed_at = html.find('id="ul-basemap-tiles"')
        core_at = html.find("dashboard/js/core")
        self.assertNotEqual(embed_at, -1, "the base template must render the catalogue")
        self.assertNotEqual(core_at, -1)
        self.assertLess(embed_at, core_at, "the catalogue must be in the document before core.js runs")

    def test_a_cold_cache_renders_nothing_rather_than_calling_redata_mid_render(self) -> None:
        """The embed is in themes/base.html, so it renders on every page - a profile, a settings form, anything with no map on it at all. A REData call costs ~1.45s (P131), and putting that inside a page render once per cache expiry would be a slow page nobody could explain."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.list_sources", return_value=[_RASTER]) as list_sources,
        ):
            rendered = _render(self.user)

        self.assertEqual(rendered.strip(), "", "a cold cache must produce no embed")
        self.assertEqual(list_sources.call_count, 0, "rendering a page must never reach REData")

    def test_the_catalogue_view_still_fetches_so_the_cache_heals_itself(self) -> None:
        """The other half of the cold path: the embed goes missing, the client falls back to its own fetch, and that request is the one allowed to pay for the refill - it is an XHR, not a page render."""
        self.client.force_login(self.user)
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.list_sources", return_value=[_RASTER]) as list_sources,
        ):
            response = self.client.get("/dashboard/map/basemap-tiles/sources/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([entry["id"] for entry in response.json()["layers"]], ["street"])
            self.assertEqual(list_sources.call_count, 1)

            # And now the embed has something to render, with no further upstream call.
            layers = _embedded(_render(self.user))

        self.assertEqual([entry["id"] for entry in layers], ["street"])
        self.assertEqual(list_sources.call_count, 1, "the render must have come from the cache the view filled")
