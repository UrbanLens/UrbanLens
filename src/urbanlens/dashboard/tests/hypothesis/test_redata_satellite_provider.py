"""Tests for RedataSatelliteProvider - the satellite carousel's REData-backed slides.

Mocks ``RedataImageryGateway`` at the plugin module's import site rather than
performing real HTTP, per this codebase's existing gateway-consumer test
convention (see e.g. ``test_nps_plugin.py``).
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.satellite_imagery import _REDATA_PROVIDER_NAMES, RedataSatelliteProvider
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

_GATEWAY_PATH = "urbanlens.dashboard.plugins.builtin.satellite_imagery.RedataImageryGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.satellite_imagery.redata_configured"
#: Patched at its definition, not at an import site: `_wanted_providers`
#: imports it inside the function to avoid a module-level cycle.
_CAPABILITIES_PATH = "urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway.applicable_providers"


class RedataSatelliteProviderTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = RedataSatelliteProvider()

    def _slides(self, results: list[dict], *, discovered: list[str] | None = None) -> list:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=discovered if discovered is not None else []),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.return_value = results
            gateway_cls.return_value.get_timeline.return_value = {}
            return list(self.provider._generate_satellite_slides(41.7, -73.9))

    def test_yields_nothing_when_redata_is_not_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False), mock.patch(_GATEWAY_PATH) as gateway_cls:
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))
        self.assertEqual(slides, [])
        gateway_cls.assert_not_called()

    def _requested(self, discovered: list[str]) -> list[str]:
        """The provider list actually sent to `/imagery/` for a discovery answer."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=discovered),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.return_value = []
            gateway_cls.return_value.get_timeline.return_value = {}
            list(self.provider._generate_satellite_slides(41.7, -73.9))
            if not gateway_cls.return_value.get_imagery.called:
                return []
            return list(gateway_cls.return_value.get_imagery.call_args.kwargs["providers"])

    def test_the_providers_asked_come_from_redatas_capability_index(self) -> None:
        """Not from a list in this repo - a source REData registers must appear."""
        requested = self._requested(["nasa_gibs", "some_source_redata_added_yesterday"])

        self.assertEqual(requested, ["nasa_gibs", "some_source_redata_added_yesterday"])

    def test_providers_another_surface_shows_better_are_left_out(self) -> None:
        requested = self._requested(
            [
                "esri_world_imagery",
                "esri_wayback",
                "usgs_imagery",
                "usgs_topo",
                "map_warper",
                "loc_sanborn",
                "nasa_gibs",
            ]
        )

        self.assertEqual(requested, ["nasa_gibs"])

    def test_sentinel_2_cloudless_is_requested(self) -> None:
        """One frame per year since 2016 - the sequence that shows a site change."""
        self.assertIn("s2cloudless", self._requested(["s2cloudless"]))
        self.assertIn("s2cloudless", _REDATA_PROVIDER_NAMES)

    def test_a_failed_discovery_falls_back_to_the_curated_list(self) -> None:
        """A capability outage must not take the carousel with it."""
        requested = self._requested([])

        self.assertEqual(set(requested), set(_REDATA_PROVIDER_NAMES))

    def test_nothing_applicable_asks_nothing_rather_than_everything(self) -> None:
        """An empty `provider` list reads as *all* providers at REData's end.

        That would fan the request out across the scanned-map collections this
        carousel deliberately leaves out, so "everything here belongs to another
        panel" has to mean no request at all - not a request with no filter.
        """
        self.assertEqual(self._requested(["map_warper", "loc_sanborn"]), [])

    def test_an_outage_propagates_so_the_caller_can_tell(self) -> None:
        """Deliberately not swallowed any more.

        Swallowing made "this place has no imagery" and "we could not ask"
        identical, and `get_satellite_slides` then cached the outage as a
        permanent absence. Letting it out is what lets that layer cache the
        first and not the second - see test_slide_outage_not_cached.py.
        """
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.side_effect = LocationContextUnavailableError("source_error", "boom")

            with self.assertRaises(LocationContextUnavailableError):
                list(self.provider._generate_satellite_slides(41.7, -73.9))

    def test_the_carousel_entry_point_still_survives_an_outage(self) -> None:
        """The property the old test was defending, asserted where it now lives."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.side_effect = LocationContextUnavailableError("source_error", "boom")

            slides, from_cache, _degraded = self.provider.get_satellite_slides(41.7, -73.9)

        self.assertEqual(slides, [])
        self.assertFalse(from_cache)

    def test_a_direct_image_delivery_uses_the_url_as_is(self) -> None:
        slides = self._slides(
            [
                {
                    "provider": "nasa_gibs",
                    "url": "https://gibs.example/tile.jpg",
                    "delivery": "image",
                    "captured_on": "2019",
                    "attribution": "NASA GIBS",
                }
            ]
        )

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].img_src, "https://gibs.example/tile.jpg")
        self.assertEqual(slides[0].source, "NASA GIBS")
        self.assertEqual(slides[0].date, "2019")
        self.assertEqual(slides[0].detail, "NASA GIBS")

    def test_a_keyed_provider_downloads_and_embeds_bytes(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.return_value = [
                {
                    "provider": "mapbox",
                    "url": "/api/v1/imagery/abc/download/",
                    "delivery": "image",
                    "captured_label": "Current",
                }
            ]
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.download_bytes.return_value = b"\xff\xd8\xff"
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertTrue(slides[0].img_src.startswith("data:image/jpeg;base64,"))
        gateway_cls.return_value.download_bytes.assert_called_once_with("/api/v1/imagery/abc/download/")

    def test_a_keyed_provider_download_failure_skips_that_slide(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = [
                {"provider": "bing_maps", "url": "/download/", "delivery": "image"},
                {
                    "provider": "nasa_gibs",
                    "url": "https://gibs.example/tile.jpg",
                    "delivery": "image",
                    "captured_on": "2019",
                },
            ]
            gateway_cls.return_value.download_bytes.side_effect = LocationContextUnavailableError(
                "source_error", "boom"
            )
            slides = list(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].source, "NASA GIBS")

    def test_a_tile_template_delivery_resolves_a_concrete_tile(self) -> None:
        slides = self._slides(
            [
                {
                    "provider": "opentopomap",
                    "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
                    "delivery": "tile_template",
                    "attributes": {"subdomains": ["a", "b", "c"]},
                }
            ]
        )

        self.assertEqual(len(slides), 1)
        img_src = slides[0].img_src
        self.assertTrue(img_src.startswith("https://a.tile.opentopomap.org/15/"))
        self.assertNotIn("{", img_src)

    def test_a_provider_with_no_display_name_still_renders(self) -> None:
        """Gating on a known name is what made a new REData source invisible.

        It reached this code twice - once because it was requested, and once as
        a historical capture the timeline returned - and was dropped both times
        for having no entry in a dict in this repo.
        """
        slides = self._slides(
            [{"provider": "some_new_provider", "url": "https://example.test/x.jpg", "delivery": "image"}],
            discovered=["some_new_provider"],
        )

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].source, "Some New Provider")

    def test_a_provider_shown_elsewhere_is_skipped_even_if_returned(self) -> None:
        """The timeline hands back rows the carousel never asked for."""
        slides = self._slides(
            [{"provider": "map_warper", "url": "https://example.test/x.jpg", "delivery": "image"}],
            discovered=["nasa_gibs"],
        )

        self.assertEqual(slides, [])

    def test_a_result_with_no_url_is_skipped(self) -> None:
        slides = self._slides([{"provider": "nasa_gibs", "url": None, "delivery": "image"}])
        self.assertEqual(slides, [])
