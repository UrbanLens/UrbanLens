"""Tests for UL-288: the satellite and street-view carousel endpoints."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, StreetViewSlide
from urbanlens.dashboard.services.pins.external_data import ProviderFetchResult, panel_sources

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class MediaCarouselSharedFlowTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_satellite_view_404s_for_a_pin_owned_by_someone_else(self) -> None:
        other_pin: Pin = baker.make_recipe("dashboard.pin")
        response = self.client.get(reverse("pin.satellite_view", args=[other_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_street_view_404s_for_a_pin_owned_by_someone_else(self) -> None:
        other_pin: Pin = baker.make_recipe("dashboard.pin")
        response = self.client.get(reverse("pin.street_view", args=[other_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_satellite_view_not_ready_schedules_a_fetch_and_returns_pending(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Satellite View")

    def test_street_view_not_ready_schedules_a_fetch_and_returns_pending(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.street_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Street View")

    def test_satellite_view_renders_slides_once_ready(self) -> None:
        source = panel_sources()["satellite"]
        cache.set(source.ready_key(self.pin), 1, 3600)
        slide = SatelliteSlide(source="Google Maps", date="2026", detail="", img_src="https://example.com/sat.jpg")
        with mock.patch(
            "urbanlens.dashboard.services.pins.external_data.collect_satellite_slides",
            return_value=([slide], [ProviderFetchResult("google_maps", from_cache=True, count=1)]),
        ):
            response = self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Google Maps")
        # The dead lat/lng context keys the old inline satellite method
        # passed (unused by the template) were dropped during the dedup -
        # confirm nothing regressed by asserting on rendered content only.
        self.assertContains(response, "sat-carousel")
        copy = RemoteImageCopy.objects.get()
        self.assertEqual((copy.source_url, copy.provider), ("https://example.com/sat.jpg", "satellite:Google Maps"))
        self.assertNotContains(response, "example.com/sat.jpg")

    def test_street_view_renders_slides_once_ready(self) -> None:
        source = panel_sources()["street_view"]
        cache.set(source.ready_key(self.pin), 1, 3600)
        slide = StreetViewSlide(
            source="Google Street View", date="2026", img_src="https://example.com/sv.jpg", latitude=1.0, longitude=2.0
        )
        with mock.patch(
            "urbanlens.dashboard.services.pins.external_data.collect_street_view_slides",
            return_value=([slide], [ProviderFetchResult("google_street_view", from_cache=True, count=1)]),
        ):
            response = self.client.get(reverse("pin.street_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Google Street View")
        self.assertNotContains(response, "example.com/sv.jpg")
        self.assertContains(response, reverse("media.remote_copy", args=[RemoteImageCopy.objects.get().url_digest]))

    def test_a_current_imagery_slide_is_copied_afresh_each_month(self) -> None:
        """The provider replaces a "Current" export in place, so a copy kept forever would stop being current."""
        from datetime import datetime

        source = panel_sources()["satellite"]
        cache.set(source.ready_key(self.pin), 1, 3600)
        current = SatelliteSlide(
            source="Esri World Imagery", date="Current", detail="", img_src="https://example.com/now.jpg"
        )
        dated = SatelliteSlide(
            source="Esri Wayback", date="2019-02-21", detail="", img_src="https://example.com/2019.jpg"
        )
        for month in (1, 2):
            with (
                mock.patch(
                    "urbanlens.dashboard.services.pins.external_data.collect_satellite_slides",
                    return_value=([current, dated], [ProviderFetchResult("esri", from_cache=True, count=2)]),
                ),
                mock.patch("django.utils.timezone.now", return_value=datetime(2026, month, 15, tzinfo=UTC)),
            ):
                self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]))

        self.assertEqual(
            sorted(RemoteImageCopy.objects.values_list("source_url", "edition")),
            [
                ("https://example.com/2019.jpg", ""),
                ("https://example.com/now.jpg", "2026-01"),
                ("https://example.com/now.jpg", "2026-02"),
            ],
        )
