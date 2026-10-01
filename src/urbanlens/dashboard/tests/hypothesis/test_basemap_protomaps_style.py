"""Which style document a vector layer resolves to when this deployment buys its basemap.

The choice is invisible in the catalogue's shape - a Protomaps-hosted entry and a self-hosted one
are both a vector layer with a ``style_url`` - so nothing downstream can catch a wrong one. What it
decides is which origin every browser on the site fetches its basemap from, and whose quota that
spends. Buying the hosted basemap resolves to *this* origin's proxy, not to the API directly: see
``controllers.basemap_tiles.VectorBasemapStyleView`` for what that is for.
"""

from __future__ import annotations

from urllib.parse import urlparse

from django.test import SimpleTestCase
from django.urls import reverse

from urbanlens.dashboard.services.map.basemap_catalogue import _offered_layers, protomaps_theme_for, vector_style_url
from urbanlens.UrbanLens.settings.app import settings as app_settings


class ProtomapsStyleUrlTests(SimpleTestCase):
    def setUp(self) -> None:
        self._original = app_settings.protomaps_api_key
        self.addCleanup(setattr, app_settings, "protomaps_api_key", self._original)

    def test_no_key_keeps_whatever_redata_published(self) -> None:
        """The default, and the whole of the self-hosting path: an unset key is what selects it."""
        app_settings.protomaps_api_key = ""

        self.assertIsNone(protomaps_theme_for("street"))
        self.assertIsNone(vector_style_url("street"))
        self.assertIsNone(vector_style_url("dark"))

    def test_a_key_points_street_and_dark_at_this_origins_proxy(self) -> None:
        app_settings.protomaps_api_key = "abc123"

        self.assertEqual(protomaps_theme_for("street"), "light")
        self.assertEqual(protomaps_theme_for("dark"), "dark")
        self.assertEqual(vector_style_url("street"), reverse("map.basemap_vector_style", kwargs={"theme": "light"}))
        self.assertEqual(vector_style_url("dark"), reverse("map.basemap_vector_style", kwargs={"theme": "dark"}))

    def test_the_key_is_not_published_in_the_style_url(self) -> None:
        """It is a quota, and it is honoured from any Origin - so a copy in a public document is a
        copy anyone can spend."""
        app_settings.protomaps_api_key = "abc123"

        self.assertNotIn("abc123", vector_style_url("street") or "")

    def test_a_layer_protomaps_does_not_publish_is_left_alone(self) -> None:
        """Terrain is a Copernicus DEM archive; answering it with a street style would draw the wrong planet."""
        app_settings.protomaps_api_key = "abc123"

        self.assertIsNone(vector_style_url("terrain"))
        self.assertIsNone(vector_style_url("satellite"))


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

        self.assertEqual(
            urlparse(str(street["style_url"])).netloc, "", "the browser must be sent to this origin, not to the meter"
        )
        self.assertTrue(str(street["style_url"]).startswith("/"))
        # The raster half is this origin's proxy either way: buying the vector basemap says nothing
        # about where a layer with no vector half comes from.
        self.assertNotIn("style_url", satellite)
        self.assertTrue(str(street["url_template"]).startswith("/"))
