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
from urbanlens.dashboard.services.core import single_flight

_MODULE = "urbanlens.dashboard.services.map.basemap_catalogue"
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

    def test_a_layer_id_the_proxy_route_cannot_carry_is_skipped_rather_than_raising(self) -> None:
        """The route captures ``<slug:layer>``, so an id outside that alphabet has no URL to build and ``reverse()`` answers with ``NoReverseMatch``. Unhandled, one such entry from REData takes the whole catalogue - and with it every map's layer strip - down with it."""
        _warm([dict(_RASTER, id="street.v2"), {**_RASTER, "id": "good"}, dict(_RASTER, id=None)])

        layers = _embedded(_render(self.user))

        self.assertEqual([entry["id"] for entry in layers], ["good"], "the usable entry must survive its neighbours")

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


class TheCatalogueIsFetchedOncePerColdWindowTests(TestCase):
    """The catalogue is cached for a day with no jitter, so it goes cold at one moment for
    everybody. Every authenticated page load's `registerRedataLayers()` XHR misses in the ~1.5s
    that refill takes (P131), and each miss holds a request thread for the whole of it: production
    runs 6 workers x 4 threads, so 24 concurrent misses is the whole site. The tile proxy got a
    concurrency bound for exactly this shape; the catalogue it advertises did not."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.map import basemap_catalogue

        cache.delete(basemap_catalogue.CATALOGUE_CACHE_KEY)
        cache.delete(basemap_catalogue.CATALOGUE_FETCH_KEY)

    def test_a_second_caller_arriving_mid_fetch_does_not_ask_redata_too(self) -> None:
        from urbanlens.dashboard.services.map.basemap_catalogue import basemap_tile_catalogue

        second: list[list[dict]] = []

        def answer_slowly(_self) -> list[dict]:
            if not second:
                # A second request lands while this one is still waiting on REData. It cannot be
                # served here - nothing else runs until this returns - so it waits and degrades,
                # which is the point: it does not make a second call.
                second.append(basemap_tile_catalogue())
            return [_RASTER]

        with (
            mock.patch(_CONFIGURED, return_value=True),
            # Nothing can publish while the winner is blocked on this very call, so the wait only
            # costs the test time.
            mock.patch(f"{_MODULE}.CATALOGUE_WAIT_SECONDS", 0.05),
            mock.patch(f"{_GATEWAY}.list_sources", autospec=True, side_effect=answer_slowly) as list_sources,
        ):
            first = basemap_tile_catalogue()

        self.assertEqual(list_sources.call_count, 1, "the second caller made its own REData call")
        self.assertEqual([entry["id"] for entry in first], ["street"])
        self.assertEqual(second, [[]], "a caller that did not fetch must degrade, not invent layers")

    def test_a_waiting_caller_is_served_the_answer_the_holder_published(self) -> None:
        """Degrading is the fallback, not the behaviour: a map that drops back to vendor tiles has
        told that vendor the coordinates the proxy exists to keep from it."""
        from urbanlens.dashboard.services.map import basemap_catalogue
        from urbanlens.dashboard.services.map.basemap_catalogue import _await_catalogue, basemap_tile_catalogue

        single_flight.claim(basemap_catalogue.CATALOGUE_FETCH_KEY, 30)
        published: list[list[dict]] = []

        def publish_midway(_name: str) -> None:
            """Stands in for the holder finishing while this caller is asleep."""
            if not published:
                with (
                    mock.patch(_CONFIGURED, return_value=True),
                    mock.patch(f"{_GATEWAY}.list_sources", return_value=[_RASTER]),
                ):
                    single_flight.release(basemap_catalogue.CATALOGUE_FETCH_KEY)
                    published.append(basemap_tile_catalogue())
                single_flight.claim(basemap_catalogue.CATALOGUE_FETCH_KEY, 30)

        with mock.patch(f"{_MODULE}.time.sleep", side_effect=publish_midway):
            waited = _await_catalogue()

        single_flight.release(basemap_catalogue.CATALOGUE_FETCH_KEY)
        self.assertEqual([entry["id"] for entry in waited], ["street"])

    def test_a_waiting_caller_stops_as_soon_as_the_holder_gives_up(self) -> None:
        """A failed refill releases the key without publishing; waiting out the rest of the budget
        after that only holds a request thread for nothing."""
        from urbanlens.dashboard.services.map import basemap_catalogue
        from urbanlens.dashboard.services.map.basemap_catalogue import _await_catalogue

        single_flight.claim(basemap_catalogue.CATALOGUE_FETCH_KEY, 30)
        naps = 0

        def give_up_after_one(_name: str) -> None:
            nonlocal naps
            naps += 1
            single_flight.release(basemap_catalogue.CATALOGUE_FETCH_KEY)

        with mock.patch(f"{_MODULE}.time.sleep", side_effect=give_up_after_one):
            self.assertEqual(_await_catalogue(), [])

        self.assertEqual(naps, 1, "it kept sleeping after the holder was gone")

    def test_the_next_cold_window_fetches_again(self) -> None:
        """Anti-vacuity: a reservation that is never released would make the catalogue
        unrefreshable for as long as its own TTL, which is the failure it is meant to prevent."""
        from urbanlens.dashboard.services.map import basemap_catalogue
        from urbanlens.dashboard.services.map.basemap_catalogue import basemap_tile_catalogue

        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.list_sources", return_value=[_RASTER]) as list_sources,
        ):
            basemap_tile_catalogue()
            cache.delete(basemap_catalogue.CATALOGUE_CACHE_KEY)
            basemap_tile_catalogue()

        self.assertEqual(list_sources.call_count, 2)

    def test_a_failed_fetch_releases_the_reservation(self) -> None:
        """An outage must not lock the catalogue cold for the reservation's whole lifetime."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
        from urbanlens.dashboard.services.map.basemap_catalogue import basemap_tile_catalogue

        with mock.patch(_CONFIGURED, return_value=True):
            with mock.patch(
                f"{_GATEWAY}.list_sources", side_effect=LocationContextUnavailableError("source_error", "down")
            ):
                self.assertEqual(basemap_tile_catalogue(), [])
            with mock.patch(f"{_GATEWAY}.list_sources", return_value=[_RASTER]) as recovered:
                self.assertEqual([entry["id"] for entry in basemap_tile_catalogue()], ["street"])

        self.assertEqual(recovered.call_count, 1)
