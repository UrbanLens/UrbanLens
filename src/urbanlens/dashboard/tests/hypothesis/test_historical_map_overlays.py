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


class GeoreferenceAccuracyTests(TestCase):
    """How well a sheet is placed - reported only where the number means something.

    `rmse_meters` is the fit's own residual, and REData's model docstring warns
    that a thin-plate spline interpolates its control points *by construction*,
    so its residual is ~0 whatever the placement is actually like. Printing
    "±0 m" for one would advertise a perfect fit for what may be the worst sheet
    in the list, which is worse than printing nothing.
    """

    def test_a_loose_polynomial_fit_reports_its_error(self) -> None:
        from urbanlens.dashboard.controllers.map_overlays import georeference_accuracy

        note = georeference_accuracy({"transformation": "polynomial", "rmse_meters": 41.6, "gcp_count": 6})

        self.assertEqual(note, "±42 m (6 control points)")

    def test_a_thin_plate_spline_reports_nothing(self) -> None:
        """Its residual is ~0 by construction, not because the placement is good."""
        from urbanlens.dashboard.controllers.map_overlays import georeference_accuracy

        self.assertEqual(georeference_accuracy({"transformation": "thinPlateSpline", "rmse_meters": 0.0, "gcp_count": 30}), "")

    def test_a_tight_fit_is_not_worth_the_pixels(self) -> None:
        """A few metres on a scanned historical map is noise, not information."""
        from urbanlens.dashboard.controllers.map_overlays import georeference_accuracy

        self.assertEqual(georeference_accuracy({"transformation": "polynomial", "rmse_meters": 3.0, "gcp_count": 8}), "")

    def test_a_missing_or_malformed_figure_reports_nothing(self) -> None:
        from urbanlens.dashboard.controllers.map_overlays import georeference_accuracy

        self.assertEqual(georeference_accuracy({}), "")
        self.assertEqual(georeference_accuracy({"transformation": "polynomial", "rmse_meters": None}), "")
        self.assertEqual(georeference_accuracy({"transformation": "polynomial", "rmse_meters": "40"}), "")

    def test_an_unknown_control_point_count_is_simply_omitted(self) -> None:
        from urbanlens.dashboard.controllers.map_overlays import georeference_accuracy

        self.assertEqual(georeference_accuracy({"transformation": "helmert", "rmse_meters": 88.0}), "±88 m")


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

    def test_the_sheet_thumbnail_and_catalogue_link_are_offered(self) -> None:
        """Choosing between a dozen scans of one neighbourhood is a visual task.

        REData caches the institution's own thumbnail and catalogue page, and
        the picker showed neither - eleven rows reading "Sanborn Map of ..." is
        not a way to pick one. Both are the *institution's* public URLs, not
        REData-authenticated ones, so unlike the tile template they need no
        proxy.
        """
        match = _match(self.georeference_uuid)
        match["sheet"]["thumbnail_url"] = "https://tile.loc.gov/thumb/sanborn-1893.jpg"
        match["sheet"]["landing_page_url"] = "https://www.loc.gov/item/sanborn01234_001/"

        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [match]
            body = self.client.get(self.url).content.decode()

        self.assertIn("https://tile.loc.gov/thumb/sanborn-1893.jpg", body)
        self.assertIn("https://www.loc.gov/item/sanborn01234_001/", body)

    def test_a_sheet_without_a_thumbnail_still_lists(self) -> None:
        """Most providers publish one; a sheet that does not must not vanish."""
        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [_match(self.georeference_uuid)]
            body = self.client.get(self.url).content.decode()

        self.assertIn("Sanborn Fire Insurance Map", body)
        self.assertNotIn("map-overlay-historical-thumb", body)

    def test_a_loose_placement_is_disclosed_in_the_list(self) -> None:
        match = _match(self.georeference_uuid)
        match["georeference"].update({"transformation": "polynomial", "rmse_meters": 60.0, "gcp_count": 4})

        with (
            mock.patch(_CONFIGURED_PATH, return_value=True),
            mock.patch(_GATEWAY_PATH) as gateway_cls,
        ):
            gateway_cls.return_value.get_maps_covering.return_value = [match]
            body = self.client.get(self.url).content.decode()

        self.assertIn("placed to ±60 m", body)

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
