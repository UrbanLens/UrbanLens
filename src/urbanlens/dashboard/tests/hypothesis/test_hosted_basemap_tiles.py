"""Where the browser fetches the street and dark basemaps' tiles: Protomaps' hosted API, directly, or our own mirror.

Production and staging hand the page a hosted tile template carrying the key (``{% hosted_basemap_tiles %}``); the
client points our own style's ``protomaps`` source at it and falls back to the style's own tiles when it fails
(``frontend/ts/shared/hosted-basemap.ts``). Development and local hand out nothing and draw our own tiles. No tile
passes through this origin either way: the proxy that used to fetch them server-side is gone.
"""

from __future__ import annotations

import logging
from unittest import mock

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import override_settings
from django.urls import NoReverseMatch, resolve, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.map.basemap_catalogue import _offered_layers, hosted_basemap_tiles

_KEY = "pk_test/+key"
_TEMPLATE = "https://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt?key=pk_test%2F%2Bkey"


def _environment(name: str):
    """This deployment, as the egress policy reads it, set to ``name`` - the test suite otherwise reads as ``testing``."""
    return override_settings(ENVIRONMENT_NAME=name, TESTING=False)


def _key(value: str):
    return mock.patch("urbanlens.UrbanLens.settings.app.settings.protomaps_api_key", value)


class HostedTilesPerEnvironmentTests(SimpleTestCase):
    def test_production_and_staging_draw_the_hosted_tiles(self) -> None:
        for environment in ("production", "staging"):
            with self.subTest(environment=environment), _environment(environment), _key(_KEY):
                self.assertEqual(hosted_basemap_tiles(), _TEMPLATE)

    def test_development_and_local_draw_our_own(self) -> None:
        """Our own tiles are internal and free; development's share of every external budget is 0."""
        for environment in ("development", "local"):
            with self.subTest(environment=environment), _environment(environment), _key(_KEY):
                self.assertIsNone(hosted_basemap_tiles())

    def test_without_a_key_every_environment_draws_our_own(self) -> None:
        for environment in ("production", "staging", "development"):
            with self.subTest(environment=environment), _environment(environment), _key(""):
                self.assertIsNone(hosted_basemap_tiles())

    def test_the_template_is_one_maplibre_can_fill_in(self) -> None:
        """Literal braces - a percent-encoded `{z}` is requested as the string `%7Bz%7D` - and the key encoded."""
        with _environment("staging"), _key(_KEY):
            template = hosted_basemap_tiles() or ""
        for token in ("{z}", "{x}", "{y}"):
            self.assertIn(token, template)
        self.assertTrue(template.startswith("https://api.protomaps.com/tiles/v4/"))
        self.assertNotIn("/+", template.partition("?")[2])


class TheEmbedTests(SimpleTestCase):
    _TAG = Template("{% load map_components %}{% hosted_basemap_tiles %}")

    def test_staging_embeds_the_hosted_template(self) -> None:
        with _environment("staging"), _key(_KEY):
            html = self._TAG.render(Context({}))
        self.assertIn('id="ul-hosted-basemap"', html)
        self.assertIn(_TEMPLATE, html)

    def test_development_embeds_nothing(self) -> None:
        with _environment("development"), _key(_KEY):
            html = self._TAG.render(Context({}))
        self.assertEqual(html, "")
        self.assertNotIn("pk_test", html)

    def test_rendering_it_logs_nothing_carrying_the_key(self) -> None:
        """The key is public in the page by design; it still has no business in this server's logs."""
        records: list[logging.LogRecord] = []

        class _Keep(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Keep(level=logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with _environment("production"), _key(_KEY):
                self._TAG.render(Context({}))
                hosted_basemap_tiles()
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        self.assertFalse([record for record in records if "pk_test" in record.getMessage()])


class TheCatalogueKeepsOurOwnStyleTests(SimpleTestCase):
    """Only the tile source moves, and it moves in the browser: the style, glyphs and sprites stay REData's."""

    @staticmethod
    def _sources() -> list[dict[str, object]]:
        return [
            {
                "id": "street",
                "name": "Street",
                "source_type": "vector",
                "attribution": "© OpenStreetMap contributors © Protomaps",
                "min_zoom": 0,
                "max_zoom": 15,
                "style_url": "https://tiles.urbanlens.org/styles/street.json",
                "url_template": "https://redata.example.test/tiles/street/{z}/{x}/{y}/",
            },
            {
                "id": "dark",
                "name": "Dark",
                "source_type": "vector",
                "attribution": "© OpenStreetMap contributors © Protomaps",
                "style_url": "https://tiles.urbanlens.org/styles/dark.json",
            },
        ]

    def test_every_environment_publishes_redatas_style(self) -> None:
        for environment in ("production", "staging", "development"):
            with self.subTest(environment=environment), _environment(environment), _key(_KEY):
                offered = {entry["id"]: entry for entry in _offered_layers(self._sources())}
                self.assertEqual(offered["street"]["style_url"], "https://tiles.urbanlens.org/styles/street.json")
                self.assertEqual(offered["dark"]["style_url"], "https://tiles.urbanlens.org/styles/dark.json")
                # The catalogue is cached for a day and served to every viewer; the key is never in it.
                self.assertNotIn("pk_test", repr(offered))


class TheProxyIsGoneTests(TestCase):
    """No tile passes through this origin: the vector tile and style proxy routes no longer exist."""

    def test_the_routes_are_not_registered(self) -> None:
        for name, kwargs in (
            ("map.basemap_vector_tiles", {"z": 1, "x": 0, "y": 0}),
            ("map.basemap_vector_style", {"theme": "light"}),
        ):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(name, kwargs=kwargs)

    def test_the_old_paths_reach_only_the_not_found_page(self) -> None:
        for path in ("/dashboard/map/basemap-vector/tiles/12/1206/1524/", "/dashboard/map/basemap-vector/light/style/"):
            with self.subTest(path=path):
                self.assertEqual(resolve(path).url_name, "404")

    def test_a_signed_in_viewer_asking_for_one_gets_a_404_and_nothing_is_fetched(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.client.force_login(baker.make(User))
        with _key(_KEY), mock.patch("requests.Session.request") as wire:
            response = self.client.get("/dashboard/map/basemap-vector/tiles/12/1206/1524/")
        self.assertEqual(response.status_code, 404)
        wire.assert_not_called()

    def test_the_views_and_gateway_are_gone(self) -> None:
        from urbanlens.dashboard.controllers import basemap_tiles

        self.assertFalse(hasattr(basemap_tiles, "VectorBasemapTileView"))
        self.assertFalse(hasattr(basemap_tiles, "VectorBasemapStyleView"))
        with self.assertRaises(ImportError):
            __import__("urbanlens.dashboard.services.apis.locations.protomaps_basemap_gateway")
