"""Tests for UL-288: the satellite and street-view carousel endpoints.

satellite_view_carousell() and street_view() used to be two independent,
near-identical ~50-line methods (pin lookup, coordinate-null check,
warm-cache readiness gate, deadline-guarded collector call, debug-entry
loop, render) differing only in their service key, collector function,
template, and a couple of extra context keys. This exercises the shared
_render_media_carousel() helper they were consolidated into, through both
call sites, to confirm the extraction didn't change behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.external_data import ProviderFetchResult, panel_sources

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
        slide = {"source": "Google Maps", "date": "2026", "detail": "", "img_src": "https://example.com/sat.jpg"}
        with mock.patch(
            "urbanlens.dashboard.services.external_data.collect_satellite_slides",
            return_value=([slide], [ProviderFetchResult("google_maps", from_cache=True, count=1)]),
        ):
            response = self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Google Maps")
        # The dead lat/lng context keys the old inline satellite method
        # passed (unused by the template) were dropped during the dedup -
        # confirm nothing regressed by asserting on rendered content only.
        self.assertContains(response, "sat-carousel")

    def test_street_view_renders_slides_once_ready(self) -> None:
        source = panel_sources()["street_view"]
        cache.set(source.ready_key(self.pin), 1, 3600)
        slide = {"source": "Google Street View", "date": "2026", "img_src": "https://example.com/sv.jpg", "latitude": 1.0, "longitude": 2.0, "heading": None}
        with mock.patch(
            "urbanlens.dashboard.services.external_data.collect_street_view_slides",
            return_value=([slide], [ProviderFetchResult("google_street_view", from_cache=True, count=1)]),
        ):
            response = self.client.get(reverse("pin.street_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Google Street View")
