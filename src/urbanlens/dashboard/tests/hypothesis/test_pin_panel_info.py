"""Tests for PinController.panel_info(), the generic dispatch for InfoPanelSource panels.

Covers the shared plumbing (404/204/pending/render paths) that every
InfoPanelSource-based plugin panel (Photon, US Census Geography, EPA
Regulated Facilities, iNaturalist, News, Building Characteristics, Recent
Seismic Activity) now relies on instead of hand-written controller methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.services.external_data import InfoPanelSource, panel_sources

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class PanelInfoDispatchTests(TestCase):
    """PinController.panel_info() - generic routing shared by every InfoPanelSource."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_unknown_panel_key_returns_404(self) -> None:
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "not_a_real_panel"]))
        self.assertEqual(response.status_code, 404)

    def test_panel_key_for_a_non_info_panel_returns_404(self) -> None:
        """core/bespoke panels (e.g. "boundary", "wikipedia") aren't reachable through this route."""
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "boundary"]))
        self.assertEqual(response.status_code, 404)

    def test_pin_owned_by_another_user_returns_404(self) -> None:
        other_pin: Pin = baker.make_recipe("dashboard.pin")
        response = self.client.get(reverse("pin.panel", args=[other_pin.slug, "photon"]))
        self.assertEqual(response.status_code, 404)

    def test_coordinate_gated_panel_returns_204_at_null_island(self) -> None:
        """(0, 0) is the "never geocoded" sentinel - effective_latitude/longitude are never
        actually None (Location.latitude/longitude are non-nullable, and immutable once
        set), so the gate checks falsiness, which only (0, 0) coordinates satisfy."""
        location: Location = baker.make("dashboard.Location", latitude=0, longitude=0)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        response = self.client.get(reverse("pin.panel", args=[pin.slug, "photon"]))
        self.assertEqual(response.status_code, 204)

    def test_ungated_panel_proceeds_at_null_island(self) -> None:
        """gdelt has no coordinate gate - it should reach the cache-miss/pending path, not 204 early."""
        location: Location = baker.make("dashboard.Location", latitude=0, longitude=0)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.panel", args=[pin.slug, "gdelt"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "News")

    def test_cache_miss_schedules_fetch_and_returns_pending_placeholder(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 200)
        fetch_task.delay.assert_called_once()
        self.assertContains(response, "Photon (OpenStreetMap)")

    def test_render_context_returning_none_yields_204(self) -> None:
        """An empty/unhelpful cached result (render_context -> None) degrades to 204."""
        LocationCache.set(self.pin.location, "photon", {}, query_key="")
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 204)

    def test_renders_the_panel_when_cached_data_is_present(self) -> None:
        LocationCache.set(
            self.pin.location,
            "photon",
            {"name": "Test Building", "osm_value": "historic_building", "city": "Poughkeepsie"},
            query_key="",
        )
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Building")
        self.assertContains(response, "Poughkeepsie")

    def test_list_shaped_panel_renders_its_items(self) -> None:
        LocationCache.set(
            self.pin.location,
            "usgs_earthquakes",
            {"events": [{"magnitude": 4.2, "place": "10km N of Nowhere", "occurred_at": "2026-01-01T00:00:00Z", "url": "https://example.com"}]},
            query_key="",
        )
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "usgs_earthquakes"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10km N of Nowhere")


class SimpleInfoPanelsRegistryTests(TestCase):
    """panel_sources() correctly classifies the migrated InfoPanelSource plugins."""

    def test_all_seven_migrated_panels_are_registered_as_info_panels(self) -> None:
        sources = panel_sources()
        expected_keys = {
            "photon",
            "census_tigerweb",
            "epa_echo",
            "inaturalist",
            "gdelt",
            "overture_building_attributes",
            "usgs_earthquakes",
        }
        for key in expected_keys:
            self.assertIn(key, sources, f"{key} missing from panel_sources()")
            self.assertIsInstance(sources[key], InfoPanelSource, f"{key} is not an InfoPanelSource")

    def test_core_panels_are_not_info_panels(self) -> None:
        sources = panel_sources()
        for key in ("boundary", "satellite", "street_view"):
            self.assertNotIsInstance(sources[key], InfoPanelSource)


class PinDetailPageSimpleInfoPanelsContextTests(TestCase):
    """PinController.view() exposes simple_info_panels for the generic template loop."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_pin_details_page_lists_migrated_panels_via_the_generic_route(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertContains(response, reverse("pin.panel", args=[self.pin.slug, "usgs_earthquakes"]))
