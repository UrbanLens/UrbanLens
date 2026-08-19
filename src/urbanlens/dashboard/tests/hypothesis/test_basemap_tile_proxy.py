"""REData's basemap layers, proxied so its API key stays server-side.

REData publishes a tile catalogue and proxies the vendors behind it, which
buys layers this application would otherwise register for itself, one
attribution source of truth, and a cache that spares the vendor a request per
pan. Nothing consumed either endpoint until now.

The behaviour worth pinning is the caching contract, because getting it wrong
is invisible: REData distinguishes "the vendor confirms no such tile" (404,
cacheable) from "the vendor could not be reached" (503, never cacheable), and
caching the latter turns a passing outage into a permanently blank region of
the map - the same defect class the outage-cache check exists for.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
#: Patched at its source module, not in the controller's namespace: the
#: controller imports it inside the request method, so there is no attribute
#: on the controller module to replace.
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"


class BasemapTileProxyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})

    def test_a_tile_is_served_and_cached(self) -> None:
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNGDATA", "image/png")) as download:
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"PNGDATA")
        self.assertEqual(second.content, b"PNGDATA")
        self.assertEqual(download.call_count, 1, "a served tile must not be re-fetched on the next pan")

    def test_a_definitive_miss_is_cached(self) -> None:
        """Most of a layer's pyramid is empty; re-asking on every pan is the cost."""
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(f"{_GATEWAY}.download_tile", return_value=(404, b"", "")) as download:
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(download.call_count, 1)

    def test_a_vendor_outage_is_never_cached(self) -> None:
        """The whole point: an outage must not become a permanently blank map."""
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(f"{_GATEWAY}.download_tile", return_value=(503, b"", "")) as download:
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(download.call_count, 2, "caching a 503 memorises an outage as 'no map here'")

    def test_a_failed_request_answers_503_rather_than_500(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(f"{_GATEWAY}.download_tile", side_effect=LocationContextUnavailableError("source_error", "down")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)

    def test_unconfigured_redata_is_a_404_not_a_crash(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_proxy_requires_a_login(self) -> None:
        self.client.logout()

        self.assertNotEqual(self.client.get(self.url).status_code, 200)


class BasemapCatalogueTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles.sources")

    def _sources(self, rows):
        return mock.patch(f"{_GATEWAY}.list_sources", return_value=rows)

    def test_layers_point_at_the_proxy_not_the_vendor(self) -> None:
        """Handing the browser REData's own template gives it a layer it cannot load."""
        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS", "url_template": "https://redata.example/api/v1/tiles/usgs-topo/{z}/{x}/{y}/"}]

        with mock.patch(_CONFIGURED, return_value=True), self._sources(rows):
            layers = self.client.get(self.url).json()["layers"]

        self.assertEqual(len(layers), 1)
        self.assertNotIn("redata.example", layers[0]["url_template"])
        self.assertIn("/dashboard/map/basemap-tiles/usgs-topo/", layers[0]["url_template"])

    def test_a_layer_without_attribution_is_not_offered(self) -> None:
        """Every vendor here requires attribution on the rendered map."""
        with mock.patch(_CONFIGURED, return_value=True), self._sources([{"id": "mystery", "name": "Mystery"}]):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

    def test_an_unreachable_catalogue_is_not_cached_as_empty(self) -> None:
        """Otherwise one bad moment costs this deployment its extra layers for a day."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(f"{_GATEWAY}.list_sources", side_effect=LocationContextUnavailableError("source_error", "down")):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]
        with mock.patch(_CONFIGURED, return_value=True), self._sources(rows):
            self.assertEqual(len(self.client.get(self.url).json()["layers"]), 1)

    def test_the_catalogue_is_cached_between_requests(self) -> None:
        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]

        with mock.patch(_CONFIGURED, return_value=True), self._sources(rows) as list_sources:
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(list_sources.call_count, 1, "documented as called once per session, not once per map load")
