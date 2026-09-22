"""Which style document a vector layer resolves to when this deployment buys its basemap.

The choice is invisible in the catalogue's shape - a Protomaps-hosted entry and a self-hosted one
are both a vector layer with a ``style_url`` - so nothing downstream can catch a wrong one. What it
decides is which CDN every browser on the site fetches its basemap from, and whose quota that spends.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase

from urbanlens.dashboard.services.map.basemap_catalogue import _offered_layers, protomaps_style_url
from urbanlens.UrbanLens.settings.app import settings as app_settings


class ProtomapsStyleUrlTests(SimpleTestCase):
    def setUp(self) -> None:
        self._original = app_settings.protomaps_api_key
        self.addCleanup(setattr, app_settings, "protomaps_api_key", self._original)

    def test_no_key_keeps_whatever_redata_published(self) -> None:
        """The default, and the whole of the self-hosting path: an unset key is what selects it."""
        app_settings.protomaps_api_key = ""

        self.assertIsNone(protomaps_style_url("street"))
        self.assertIsNone(protomaps_style_url("dark"))

    def test_a_key_points_street_and_dark_at_the_hosted_api(self) -> None:
        app_settings.protomaps_api_key = "abc123"

        street = urlparse(protomaps_style_url("street") or "")
        dark = urlparse(protomaps_style_url("dark") or "")

        self.assertEqual(street.netloc, "api.protomaps.com")
        self.assertEqual(street.path, "/styles/v5/light/en.json")
        self.assertEqual(parse_qs(street.query)["key"], ["abc123"])
        self.assertEqual(dark.path, "/styles/v5/dark/en.json")

    def test_a_layer_protomaps_does_not_publish_is_left_alone(self) -> None:
        """Terrain is a Copernicus DEM archive; answering it with a street style would draw the wrong planet."""
        app_settings.protomaps_api_key = "abc123"

        self.assertIsNone(protomaps_style_url("terrain"))
        self.assertIsNone(protomaps_style_url("satellite"))

    def test_a_key_needing_escaping_does_not_break_out_of_the_query(self) -> None:
        app_settings.protomaps_api_key = "a&b=c d"

        parsed = urlparse(protomaps_style_url("street") or "")

        self.assertEqual(parse_qs(parsed.query), {"key": ["a&b=c d"]})


class OfferedLayersPrefersTheHostedStyleTests(SimpleTestCase):
    """The swap has to happen where the catalogue is built, not at the call site, or the embed and the HTTP catalogue disagree about where the basemap lives."""

    def setUp(self) -> None:
        self._original = app_settings.protomaps_api_key
        self.addCleanup(setattr, app_settings, "protomaps_api_key", self._original)

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
                "style_url": "https://tiles.example.test/styles/street.json",
                "url_template": "https://redata.example.test/tiles/street/{z}/{x}/{y}/",
            },
            {
                "id": "satellite",
                "name": "Satellite",
                "source_type": "raster",
                "attribution": "Esri",
                "min_zoom": 0,
                "max_zoom": 19,
                "url_template": "https://redata.example.test/tiles/satellite/{z}/{x}/{y}/",
            },
        ]

    def test_without_a_key_the_published_style_survives(self) -> None:
        app_settings.protomaps_api_key = ""

        street = next(e for e in _offered_layers(self._sources()) if e["id"] == "street")

        self.assertEqual(street["style_url"], "https://tiles.example.test/styles/street.json")

    def test_with_a_key_the_hosted_style_replaces_it(self) -> None:
        app_settings.protomaps_api_key = "abc123"

        offered = _offered_layers(self._sources())
        street = next(e for e in offered if e["id"] == "street")
        satellite = next(e for e in offered if e["id"] == "satellite")

        self.assertEqual(urlparse(str(street["style_url"])).netloc, "api.protomaps.com")
        # The raster half is this origin's proxy either way: buying the vector basemap says nothing
        # about where a layer with no vector half comes from.
        self.assertNotIn("style_url", satellite)
        self.assertTrue(str(street["url_template"]).startswith("/"))
