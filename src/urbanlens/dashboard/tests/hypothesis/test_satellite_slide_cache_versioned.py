"""A provider whose slide URLs change shape stops serving the old ones on deploy, not a day later.

Esri's Wayback slides moved from an export endpoint Esri does not serve to release tiles; the
coordinate-keyed cache kept handing out the broken URLs for its full lifetime."""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.esri import EsriGateway

_LAT, _LNG = 41.73328, -73.92812


class SlideCacheVersionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_slides_cached_under_the_unversioned_key_are_not_served(self) -> None:
        stale_key = make_cache_key("satellite_view_esri", f"{_LAT:.5f}", f"{_LNG:.5f}")
        cache.set(stale_key, ["https://wayback.maptiles.arcgis.com/.../MapServer/export?..."], 3600)

        with patch.object(EsriGateway, "_generate_satellite_slides", return_value=iter([])) as generate:
            fetched = EsriGateway().get_satellite_slides(_LAT, _LNG)

        self.assertFalse(fetched.from_cache)
        generate.assert_called_once()

    def test_the_versioned_entry_is_still_a_cache(self) -> None:
        with patch.object(EsriGateway, "_generate_satellite_slides", return_value=iter([])) as generate:
            EsriGateway().get_satellite_slides(_LAT, _LNG)
            second = EsriGateway().get_satellite_slides(_LAT, _LNG)

        self.assertTrue(second.from_cache)
        generate.assert_called_once()
