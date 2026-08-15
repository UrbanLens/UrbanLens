"""Tests for REData historical-map tile overlays (2026-08-15).

Covers the tile proxy's status-contract caching (200/404 cached, 503 never),
the browse/add flow creating a locked tile overlay whose template points at
UrbanLens's own proxy, and the model changes that let a tile overlay render.
"""

from __future__ import annotations

from unittest import mock
import uuid as uuid_module

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.profile.model import Profile

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway.RedataHistoricalMapsGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"


def _match(georeference_uuid: str, *, title: str = "Sanborn Fire Insurance Map", bounds: list[float] | None = None) -> dict:
    return {
        "sheet": {"title": title, "date_text": "1893", "kind": "fire_insurance", "attribution": "Library of Congress"},
        "georeference": {"uuid": georeference_uuid, "bounds": bounds or [-71.06, 42.35, -71.05, 42.36], "tile_url_template": "https://redata.example/t/{z}/{x}/{y}.png"},
        "contains_point": True,
        "distance_meters": 0.0,
    }


class HistoricalMapTileProxyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.georeference_uuid = str(uuid_module.uuid4())
        self.url = reverse("map.historical_tiles", args=[self.georeference_uuid, 14, 4956, 6058])

    def _get(self, status: int = 200, body: bytes = b"png-bytes") -> tuple:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.download_tile.return_value = (status, body, "image/png")
            first = self.client.get(self.url)
            second = self.client.get(self.url)
        return first, second, gateway_cls.return_value.download_tile

    def test_a_served_tile_is_cached(self) -> None:
        first, second, download = self._get()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"png-bytes")
        self.assertEqual(second.status_code, 200)
        download.assert_called_once()

    def test_a_definitive_404_is_cached(self) -> None:
        """Most of a sheet's bounding pyramid is outside its mask - caching the misses matters."""
        first, second, download = self._get(status=404, body=b"")
        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        download.assert_called_once()

    def test_a_503_is_never_cached(self) -> None:
        """An institutional outage must be retried, not memorised as 'no map here'."""
        first, second, download = self._get(status=503, body=b"")
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        self.assertEqual(download.call_count, 2)

    def test_login_is_required(self) -> None:
        self.client.logout()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (301, 302))

    def tearDown(self) -> None:
        cache.clear()
        super().tearDown()


class HistoricalMapBrowseTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=42.355, longitude=-71.055)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        self.url = reverse("pin.overlays.historical", args=[self.pin.slug])
        self.georeference_uuid = str(uuid_module.uuid4())

    def test_get_lists_covering_sheets(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [_match(self.georeference_uuid)]
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Sanborn Fire Insurance Map", body)
        self.assertIn(self.georeference_uuid, body)

    def test_post_creates_a_locked_tile_overlay_pointing_at_the_proxy(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [_match(self.georeference_uuid)]
            response = self.client.post(self.url, data={"georeference_uuid": self.georeference_uuid})

        self.assertEqual(response.status_code, 200)
        overlay = MapImageOverlay.objects.get(parent_pin=self.pin)
        self.assertTrue(overlay.locked, "a pre-georeferenced overlay must not offer corner dragging")
        self.assertIn(self.georeference_uuid, overlay.tile_url_template)
        self.assertTrue(overlay.tile_url_template.endswith("/{z}/{x}/{y}.png"))
        self.assertNotIn("redata.example", overlay.tile_url_template, "REData's own tile URL (whose fetches need the API key) must never be stored for the browser")
        # Corners record the georeference bounds: NW, NE, SE, SW.
        self.assertEqual(overlay.corners(), [[42.36, -71.06], [42.36, -71.05], [42.35, -71.05], [42.35, -71.06]])
        self.assertEqual(overlay.name, "Sanborn Fire Insurance Map - 1893")

    def test_post_with_a_uuid_not_covering_this_location_creates_nothing(self) -> None:
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [_match(self.georeference_uuid)]
            response = self.client.post(self.url, data={"georeference_uuid": str(uuid_module.uuid4())})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.filter(parent_pin=self.pin).exists())

    def test_post_with_a_match_missing_bounds_errors_cleanly(self) -> None:
        """A georeference without a bounds array must refuse, not 500 on unpacking."""
        broken = _match(self.georeference_uuid)
        broken["georeference"]["bounds"] = None
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [broken]
            response = self.client.post(self.url, data={"georeference_uuid": self.georeference_uuid})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.filter(parent_pin=self.pin).exists())

    def test_tile_overlays_are_renderable_and_serialized(self) -> None:
        overlay = MapImageOverlay(
            tile_url_template="/dashboard/map/historical-tiles/abc/{z}/{x}/{y}.png",
            profile=self.profile,
            parent_pin=self.pin,
        )
        overlay.set_corners([[42.36, -71.06], [42.36, -71.05], [42.35, -71.05], [42.35, -71.06]])
        overlay.save()

        self.assertIn(overlay.pk, MapImageOverlay.objects.renderable().values_list("pk", flat=True))
        self.assertEqual(overlay.to_json()["tile_url_template"], overlay.tile_url_template)
