"""Tests for RedataSatelliteProvider - the satellite carousel's REData-backed slides.

Mocks ``RedataImageryGateway`` at the plugin module's import site rather than
performing real HTTP, per this codebase's existing gateway-consumer test
convention (see e.g. ``test_nps_plugin.py``).
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.satellite_imagery import RedataSatelliteProvider
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

_GATEWAY_PATH = "urbanlens.dashboard.plugins.builtin.satellite_imagery.RedataImageryGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.satellite_imagery.redata_configured"


class RedataSatelliteProviderTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = RedataSatelliteProvider()

    def _slides(self, results: list[dict]) -> list:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.get_imagery.return_value = results
            return list(self.provider._generate_satellite_slides(41.7, -73.9))

    def test_yields_nothing_when_redata_is_not_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False), mock.patch(_GATEWAY_PATH) as gateway_cls:
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))
        self.assertEqual(slides, [])
        gateway_cls.assert_not_called()

    def test_requests_only_the_non_esri_providers(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.get_imagery.return_value = []
            list(self.provider._generate_satellite_slides(41.7, -73.9))

        requested = gateway_cls.return_value.get_imagery.call_args.kwargs["providers"]
        self.assertEqual(set(requested), {"open_aerial_map", "nasa_gibs", "opentopomap", "mapbox", "bing_maps", "azure_maps"})
        self.assertNotIn("esri_world_imagery", requested)
        self.assertNotIn("esri_wayback", requested)
        self.assertNotIn("usgs_imagery", requested)
        self.assertNotIn("usgs_topo", requested)

    def test_yields_nothing_when_redata_is_unavailable(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.get_imagery.side_effect = LocationContextUnavailableError("source_error", "boom")
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))
        self.assertEqual(slides, [])

    def test_a_direct_image_delivery_uses_the_url_as_is(self) -> None:
        slides = self._slides([{"provider": "nasa_gibs", "url": "https://gibs.example/tile.jpg", "delivery": "image", "captured_on": "2019", "attribution": "NASA GIBS"}])

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].img_src, "https://gibs.example/tile.jpg")
        self.assertEqual(slides[0].source, "NASA GIBS")
        self.assertEqual(slides[0].date, "2019")
        self.assertEqual(slides[0].detail, "NASA GIBS")

    def test_a_keyed_provider_downloads_and_embeds_bytes(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.get_imagery.return_value = [{"provider": "mapbox", "url": "/api/v1/imagery/abc/download/", "delivery": "image", "captured_label": "Current"}]
            gateway_cls.return_value.download_bytes.return_value = b"\xff\xd8\xff"
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertTrue(slides[0].img_src.startswith("data:image/jpeg;base64,"))
        gateway_cls.return_value.download_bytes.assert_called_once_with("/api/v1/imagery/abc/download/")

    def test_a_keyed_provider_download_failure_skips_that_slide(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.get_imagery.return_value = [
                {"provider": "bing_maps", "url": "/download/", "delivery": "image"},
                {"provider": "nasa_gibs", "url": "https://gibs.example/tile.jpg", "delivery": "image", "captured_on": "2019"},
            ]
            gateway_cls.return_value.download_bytes.side_effect = LocationContextUnavailableError("source_error", "boom")
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].source, "NASA GIBS")

    def test_a_tile_template_delivery_resolves_a_concrete_tile(self) -> None:
        slides = self._slides([{"provider": "opentopomap", "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "delivery": "tile_template", "attributes": {"subdomains": ["a", "b", "c"]}}])

        self.assertEqual(len(slides), 1)
        img_src = slides[0].img_src
        self.assertTrue(img_src.startswith("https://a.tile.opentopomap.org/15/"))
        self.assertNotIn("{", img_src)

    def test_an_unrecognized_provider_is_skipped(self) -> None:
        slides = self._slides([{"provider": "some_new_provider", "url": "https://example.test/x.jpg", "delivery": "image"}])
        self.assertEqual(slides, [])

    def test_a_result_with_no_url_is_skipped(self) -> None:
        slides = self._slides([{"provider": "nasa_gibs", "url": None, "delivery": "image"}])
        self.assertEqual(slides, [])
