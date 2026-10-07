"""Tests for RedataSatelliteProvider - the satellite carousel's REData-backed slides."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.core.cache import cache

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.satellite_imagery import _REDATA_PROVIDER_NAMES, RedataSatelliteProvider
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SlideFetch, SlideSignal
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_GATEWAY_PATH = "urbanlens.dashboard.plugins.builtin.satellite_imagery.RedataImageryGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.satellite_imagery.redata_configured"
#: Patched at its definition, not at an import site: `_wanted_providers`
#: imports it inside the function to avoid a module-level cycle.
_CAPABILITIES_PATH = "urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway.applicable_providers"


def _complete_slides(generated: Iterable[SatelliteSlide | SlideSignal]) -> list[SatelliteSlide]:
    """What a complete answer generated: slides only, never a signal that it was partial."""
    found = list(generated)
    slides = [item for item in found if isinstance(item, SatelliteSlide)]
    if len(slides) != len(found):
        raise AssertionError(f"a complete answer signalled it was partial: {found}")
    return slides


def _partial_slides(generated: Iterable[SatelliteSlide | SlideSignal]) -> list[SatelliteSlide]:
    """The slides an answer that an outage cost a slide generated, having checked it said so."""
    found = list(generated)
    if SlideSignal.PARTIAL not in found:
        raise AssertionError(f"an answer an outage cost a slide did not say it was partial: {found}")
    return [item for item in found if isinstance(item, SatelliteSlide)]


def _answered(results: list[dict]) -> LocationContextEnvelope:
    """REData's complete ``/imagery/`` answer."""
    return LocationContextEnvelope(count=len(results), complete=True, results=results)


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
            gateway_cls.return_value.get_imagery.return_value = _answered(results)
            gateway_cls.return_value.get_timeline.return_value = {}
            return _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

    def test_yields_nothing_when_redata_is_not_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False), mock.patch(_GATEWAY_PATH) as gateway_cls:
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))
        self.assertEqual(slides, [])
        gateway_cls.assert_not_called()

    def _requested(self, discovered: list[str]) -> list[str]:
        """The provider list actually sent to `/imagery/` for a discovery answer."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=discovered),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.return_value = _answered([])
            gateway_cls.return_value.get_timeline.return_value = {}
            _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))
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

        That would fan the request out across the scanned-map collections this carousel deliberately leaves out,
        so "everything here belongs to another panel" has to mean no request at all - not a request with no
        filter."""
        self.assertEqual(self._requested(["map_warper", "loc_sanborn"]), [])

    def test_an_outage_propagates_so_the_caller_can_tell(self) -> None:
        """Deliberately not swallowed any more.

        Swallowing made "this place has no imagery" and "we could not ask" identical, and `get_satellite_slides`
        then cached the outage as a permanent absence."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.side_effect = LocationContextUnavailableError("source_error", "boom")

            with self.assertRaises(LocationContextUnavailableError):
                _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

    def test_the_carousel_entry_point_still_survives_an_outage(self) -> None:
        """The property the old test was defending, asserted where it now lives."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.side_effect = LocationContextUnavailableError("source_error", "boom")

            fetched = self.provider.get_satellite_slides(41.7, -73.9)

        self.assertEqual(fetched.slides, [])
        self.assertFalse(fetched.from_cache)

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
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "mapbox",
                        "url": "/api/v1/imagery/abc/download/",
                        "delivery": "image",
                        "captured_label": "Current",
                    }
                ]
            )
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.download_bytes.return_value = b"\xff\xd8\xff"
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

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
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {"provider": "bing_maps", "url": "/download/", "delivery": "image"},
                    {
                        "provider": "nasa_gibs",
                        "url": "https://gibs.example/tile.jpg",
                        "delivery": "image",
                        "captured_on": "2019",
                    },
                ]
            )
            gateway_cls.return_value.download_bytes.side_effect = LocationContextUnavailableError(
                "source_error", "boom"
            )
            slides = _partial_slides(self.provider._generate_satellite_slides(41.7, -73.9))

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

        It reached this code twice - once because it was requested, and once as a historical capture the
        timeline returned - and was dropped both times for having no entry in a dict in this repo."""
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

    def test_a_tile_template_with_a_uuid_composes_a_real_photo(self) -> None:
        """FIX A: prefer REData's server-side composite over a raw tile substitution."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "opentopomap",
                        "uuid": "tile-asset-uuid",
                        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
                        "delivery": "tile_template",
                        "attributes": {"subdomains": ["a", "b", "c"]},
                    }
                ]
            )
            gateway_cls.return_value.download_archived_copy.return_value = b"\xff\xd8\xff"
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertTrue(slides[0].img_src.startswith("data:image/jpeg;base64,"))
        gateway_cls.return_value.download_archived_copy.assert_called_once_with(
            "tile-asset-uuid", width=1024, height=1024, zoom=None
        )
        gateway_cls.return_value.download_bytes.assert_not_called()

    def test_a_tile_template_with_no_uuid_falls_back_to_a_raw_tile(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "opentopomap",
                        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
                        "delivery": "tile_template",
                        "attributes": {"subdomains": ["a"]},
                    }
                ]
            )
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertTrue(slides[0].img_src.startswith("https://a.tile.opentopomap.org/15/"))
        gateway_cls.return_value.download_archived_copy.assert_not_called()

    def test_a_tile_template_composed_download_failure_falls_back_to_a_raw_tile(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "opentopomap",
                        "uuid": "tile-asset-uuid",
                        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
                        "delivery": "tile_template",
                        "attributes": {"subdomains": ["a"]},
                    }
                ]
            )
            gateway_cls.return_value.download_archived_copy.side_effect = LocationContextUnavailableError(
                "source_error", "boom"
            )
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertTrue(slides[0].img_src.startswith("https://a.tile.opentopomap.org/15/"))

    def test_a_time_series_result_materializes_the_most_recent_date(self) -> None:
        """FIX B: NASA GIBS-style time_series providers now yield a slide instead of nothing."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "uuid": "gibs-layer-uuid",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {"intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]},
                        "attribution": "NASA GIBS",
                    }
                ]
            )
            gateway_cls.return_value.capture_time_series.return_value = {"uuid": "materialized-uuid"}
            gateway_cls.return_value.download_archived_copy.return_value = b"\xff\xd8\xff"
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].source, "NASA GIBS")
        self.assertEqual(slides[0].date, "2026-08-06")
        self.assertTrue(slides[0].img_src.startswith("data:image/jpeg;base64,"))
        gateway_cls.return_value.capture_time_series.assert_called_once()
        call_args = gateway_cls.return_value.capture_time_series.call_args
        self.assertEqual(call_args.args[0], "gibs-layer-uuid")
        self.assertEqual(call_args.args[1].isoformat(), "2026-08-06")
        gateway_cls.return_value.download_archived_copy.assert_called_once_with(
            "materialized-uuid", width=1024, height=1024, zoom=None
        )

    def test_a_time_series_result_picks_the_latest_across_multiple_intervals(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "uuid": "gibs-layer-uuid",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {
                            "intervals": [
                                {"start": "2000-02-24", "end": "2013-03-21", "step": "P1D"},
                                {"start": "2013-03-22", "end": "2026-08-06", "step": "P1D"},
                            ]
                        },
                    }
                ]
            )
            gateway_cls.return_value.capture_time_series.return_value = {"uuid": "materialized-uuid"}
            gateway_cls.return_value.download_archived_copy.return_value = b"bytes"
            _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        call_args = gateway_cls.return_value.capture_time_series.call_args
        self.assertEqual(call_args.args[1].isoformat(), "2026-08-06")

    def test_a_time_series_result_with_no_uuid_is_skipped_without_calling_capture(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {"intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]},
                    }
                ]
            )
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(slides, [])
        gateway_cls.return_value.capture_time_series.assert_not_called()

    def test_a_time_series_result_with_no_intervals_is_skipped(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "uuid": "gibs-layer-uuid",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {},
                    }
                ]
            )
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(slides, [])
        gateway_cls.return_value.capture_time_series.assert_not_called()

    def test_a_time_series_capture_failure_is_skipped_without_crashing_the_panel(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "uuid": "gibs-layer-uuid",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {"intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]},
                    },
                    {
                        "provider": "esri_world_imagery_placeholder",
                        "url": "https://example.test/current.jpg",
                        "delivery": "image",
                    },
                ]
            )
            gateway_cls.return_value.capture_time_series.side_effect = LocationContextUnavailableError(
                "rate_limited", "back off"
            )
            slides = _partial_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].source, "Esri World Imagery Placeholder")

    def test_a_time_series_capture_documented_none_result_is_skipped_silently(self) -> None:
        """`capture_time_series` returning None (date_unavailable/no_imagery) is not an error."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "uuid": "gibs-layer-uuid",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {"intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]},
                    }
                ]
            )
            gateway_cls.return_value.capture_time_series.return_value = None
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(slides, [])
        gateway_cls.return_value.download_archived_copy.assert_not_called()

    def test_a_time_series_download_failure_after_a_successful_capture_is_skipped(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_timeline.return_value = {}
            gateway_cls.return_value.get_imagery.return_value = _answered(
                [
                    {
                        "provider": "nasa_gibs",
                        "uuid": "gibs-layer-uuid",
                        "url": "https://gibs.example/wms?TIME={time}",
                        "delivery": "time_series",
                        "attributes": {"intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]},
                    }
                ]
            )
            gateway_cls.return_value.capture_time_series.return_value = {"uuid": "materialized-uuid"}
            gateway_cls.return_value.download_archived_copy.side_effect = LocationContextUnavailableError(
                "source_error", "boom"
            )
            slides = _partial_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(slides, [])


class NativeResolutionZoomTests(SimpleTestCase):
    """P232: a coarse source is composed at the deepest zoom its own resolution supports, showing a wider area
    rather than enlarging its pixels; a sharp one keeps REData's default framing."""

    def setUp(self) -> None:
        super().setUp()
        self.provider = RedataSatelliteProvider()

    def _gateway_for(self, results: list[dict]):
        patches = (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=[]),
            mock.patch(_GATEWAY_PATH),
        )
        for patch in patches[:2]:
            patch.start()
            self.addCleanup(patch.stop)
        gateway_cls = patches[2].start()
        self.addCleanup(patches[2].stop)
        gateway = gateway_cls.return_value
        gateway.get_timeline.return_value = {}
        gateway.get_imagery.return_value = _answered(results)
        gateway.download_archived_copy.return_value = b"\xff\xd8\xff"
        gateway.capture_time_series.return_value = {"uuid": "materialized-uuid"}
        return gateway

    @staticmethod
    def _tiles(**fields) -> dict:
        return {
            "provider": "s2cloudless",
            "uuid": "s2-asset-uuid",
            "url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg",
            "delivery": "tile_template",
            "attributes": {},
            **fields,
        }

    def _zoom_asked(self, result: dict) -> int | None:
        gateway = self._gateway_for([result])
        _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))
        return gateway.download_archived_copy.call_args.kwargs["zoom"]

    def test_sentinel_2_is_composed_at_zoom_13_not_enlarged(self) -> None:
        self.assertEqual(self._zoom_asked(self._tiles(resolution_meters=10.0)), 13)

    def test_a_sub_metre_source_keeps_redatas_default_framing(self) -> None:
        self.assertIsNone(self._zoom_asked(self._tiles(provider="open_aerial_map", resolution_meters=0.3)))

    def test_a_source_with_no_published_resolution_keeps_redatas_default(self) -> None:
        self.assertIsNone(self._zoom_asked(self._tiles()))

    def test_a_resolution_that_is_not_a_positive_number_is_ignored(self) -> None:
        for value in (0, -5, "ten", None, float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(self._zoom_asked(self._tiles(resolution_meters=value)))

    def test_the_layers_own_shallowest_zoom_wins_over_the_resolution(self) -> None:
        self.assertEqual(self._zoom_asked(self._tiles(resolution_meters=10.0, min_zoom=14)), 14)

    def test_a_ceiling_below_the_native_zoom_is_respected(self) -> None:
        """Past a release's own max zoom every tile 404s, and REData reports a blank mosaic as no imagery."""
        self.assertEqual(
            self._zoom_asked(
                self._tiles(provider="open_aerial_map", resolution_meters=1.0, attributes={"max_map_level": 15})
            ),
            None,
        )
        self.assertEqual(self._zoom_asked(self._tiles(resolution_meters=2.0, max_zoom=14)), None)
        self.assertEqual(self._zoom_asked(self._tiles(resolution_meters=10.0, max_zoom=12)), None)

    def test_a_dated_capture_from_the_timeline_is_zoomed_the_same_way(self) -> None:
        gateway = self._gateway_for([])
        gateway.get_timeline.return_value = {"results": []}
        capture = self._tiles(uuid="s2-2019-uuid", resolution_meters=10.0, captured_on="2019-01-01")
        with mock.patch(
            "urbanlens.dashboard.services.locations.imagery_timeline.flatten_timeline",
            return_value=[{"kind": "capture", "asset": capture, "captured_on": "2019-01-01", "date_is_exact": True}],
        ):
            slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertEqual(len(slides), 1)
        self.assertEqual(gateway.download_archived_copy.call_args.kwargs["zoom"], 13)

    def test_a_time_series_capture_is_zoomed_by_its_resolution(self) -> None:
        gateway = self._gateway_for(
            [
                {
                    "provider": "nasa_gibs",
                    "uuid": "gibs-layer-uuid",
                    "url": "https://gibs.example/wms?TIME={time}",
                    "delivery": "time_series",
                    "resolution_meters": 250.0,
                    "attributes": {"intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]},
                }
            ]
        )
        _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        gateway.download_archived_copy.assert_called_once_with("materialized-uuid", width=1024, height=1024, zoom=8)

    def test_the_raw_tile_fallback_is_no_deeper_than_the_source_supports(self) -> None:
        gateway = self._gateway_for([self._tiles(resolution_meters=10.0)])
        gateway.download_archived_copy.side_effect = LocationContextUnavailableError("source_error", "boom")

        slides = _complete_slides(self.provider._generate_satellite_slides(41.7, -73.9))

        self.assertIn("/GoogleMapsCompatible/13/", slides[0].img_src)


_IMAGE = {
    "provider": "nasa_gibs",
    "url": "https://gibs.example/tile.jpg",
    "delivery": "image",
    "captured_on": "2019",
    "attribution": "NASA GIBS",
}


class PartialImageryTests(TestCase):
    """What REData answered only in part is shown, but not kept: the carousel asks again on its failure cadence."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def _fetch_twice(self, imagery: LocationContextEnvelope, timeline: object) -> tuple[list[SlideFetch], int]:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=["nasa_gibs"]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.return_value = imagery
            if isinstance(timeline, Exception):
                gateway_cls.return_value.get_timeline.side_effect = timeline
            else:
                gateway_cls.return_value.get_timeline.return_value = timeline
            fetched = [RedataSatelliteProvider().get_satellite_slides(41.7, -73.9) for _ in range(2)]
            return fetched, gateway_cls.return_value.get_imagery.call_count

    def assert_shown_and_not_kept(self, fetched: list[SlideFetch], asked: int) -> None:
        self.assertEqual([slide.img_src for slide in fetched[0].slides], [_IMAGE["url"]])
        self.assertTrue(fetched[0].degraded)
        self.assertEqual(asked, 2, "a partial answer is asked for again, not served from the slide cache")

    def test_imagery_with_a_provider_unanswered_is_shown_and_not_kept(self) -> None:
        partial = LocationContextEnvelope(
            count=1, complete=False, results=[_IMAGE], providers=[{"provider": "s2cloudless", "status": "unavailable"}]
        )

        self.assert_shown_and_not_kept(*self._fetch_twice(partial, {}))

    def test_imagery_whose_timeline_did_not_answer_is_shown_and_not_kept(self) -> None:
        self.assert_shown_and_not_kept(
            *self._fetch_twice(_answered([_IMAGE]), LocationContextUnavailableError("source_error", "503"))
        )

    def test_imagery_whose_timeline_redata_says_is_incomplete_is_shown_and_not_kept(self) -> None:
        timeline = {
            "captures": [],
            "complete": False,
            "providers": [{"provider": "esri_wayback", "status": "rate_limited"}],
        }

        self.assert_shown_and_not_kept(*self._fetch_twice(_answered([_IMAGE]), timeline))

    def _with_a_keyed_download(self, failure: Exception) -> list[SlideFetch]:
        keyed = {"provider": "bing_maps", "url": "/download/", "delivery": "image"}
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_CAPABILITIES_PATH, return_value=["nasa_gibs", "bing_maps"]),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_imagery.return_value = _answered([_IMAGE, keyed])
            gateway_cls.return_value.get_timeline.return_value = {"captures": [], "complete": True}
            gateway_cls.return_value.download_bytes.side_effect = failure
            return [RedataSatelliteProvider().get_satellite_slides(41.7, -73.9) for _ in range(2)]

    def test_a_slide_an_outage_cost_is_not_kept_without_it(self) -> None:
        """Review finding 3: a keyed provider's download that REData could not serve for now."""
        for failure in (
            LocationContextUnavailableError("source_error", "upstream timed out", status_code=503),
            LocationContextUnavailableError("source_error", "Could not reach REData"),
        ):
            with self.subTest(failure=failure):
                cache.clear()
                fetched = self._with_a_keyed_download(failure)

                self.assertEqual([slide.img_src for slide in fetched[0].slides], [_IMAGE["url"]])
                self.assertTrue(fetched[0].degraded)
                self.assertFalse(fetched[1].from_cache)

    def test_a_slide_redata_has_no_image_for_is_kept_as_a_gap(self) -> None:
        fetched = self._with_a_keyed_download(LocationContextUnavailableError("source_error", "404", status_code=404))

        self.assertFalse(fetched[0].degraded)
        self.assertTrue(fetched[1].from_cache)

    def test_a_complete_answer_is_kept(self) -> None:
        fetched, asked = self._fetch_twice(_answered([_IMAGE]), {"captures": [], "complete": True})

        self.assertFalse(fetched[0].degraded)
        self.assertTrue(fetched[1].from_cache)
        self.assertEqual(asked, 1)
