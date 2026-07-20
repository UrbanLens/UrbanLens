"""Tests for the Location Data card's "Overview" tab.

Covers PinController.location_data_overview() (the aggregation endpoint) and
_location_data_overview_section() (the per-source adapter that turns
Nominatim's bespoke ``place`` dict and every other InfoPanelSource's
render_context() output into one uniform shape - see docs/prompts/completed.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.pin import PinController
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.external_data import LocationCachePanelSource, get_panel_source

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


def _get_location_cache_panel(key: str) -> LocationCachePanelSource:
    source = get_panel_source(key)
    assert isinstance(source, LocationCachePanelSource)
    return source


class LocationDataOverviewSectionAdapterTests(SimpleTestCase):
    """_location_data_overview_section() - pure data-shape adaptation, no DB needed."""

    def setUp(self) -> None:
        super().setUp()
        self.controller = PinController()
        self.pin = Pin()  # unused by every current InfoPanelSource's render_context

    def test_generic_info_panel_source_passes_through_render_context(self) -> None:
        photon = _get_location_cache_panel("photon")
        section = self.controller._location_data_overview_section(self.pin, photon, {"name": "Test Place", "osm_value": "cafe", "city": "Poughkeepsie"})
        assert section is not None
        self.assertEqual(section["heading_name"], "Test Place")
        self.assertEqual(section["icon"], photon.icon)
        self.assertEqual(section["title"], photon.title)

    def test_generic_info_panel_source_none_render_context_yields_none(self) -> None:
        photon = _get_location_cache_panel("photon")
        self.assertIsNone(self.controller._location_data_overview_section(self.pin, photon, {}))

    def test_nominatim_with_no_name_yields_none(self) -> None:
        nominatim = _get_location_cache_panel("nominatim")
        self.assertIsNone(self.controller._location_data_overview_section(self.pin, nominatim, {}))

    def test_nominatim_adapts_place_dict_into_generic_shape(self) -> None:
        nominatim = _get_location_cache_panel("nominatim")
        place = {
            "name": "Test Cafe",
            "kind_label": "Cafe",
            "website": "https://example.test",
            "phone": "555-0100",
            "opening_hours": "Mo-Fr 08:00-18:00",
            "operator": "Test Operator",
            "osm_url": "https://openstreetmap.org/node/1",
        }
        section = self.controller._location_data_overview_section(self.pin, nominatim, place)
        assert section is not None
        self.assertEqual(section["heading_name"], "Test Cafe")
        self.assertEqual(section["chips"], ["Cafe"])
        self.assertEqual(section["title"], "Nominatim")
        fact_texts = [f["text"] for f in section["facts"]]
        self.assertIn("https://example.test", fact_texts)
        self.assertIn("555-0100", fact_texts)
        self.assertEqual(section["footer_link"]["url"], "https://openstreetmap.org/node/1")

    def test_nominatim_with_name_only_has_no_facts_or_footer(self) -> None:
        nominatim = _get_location_cache_panel("nominatim")
        section = self.controller._location_data_overview_section(self.pin, nominatim, {"name": "Bare Place"})
        assert section is not None
        self.assertEqual(section["facts"], [])
        self.assertIsNone(section["footer_link"])


class LocationDataOverviewEndpointTests(TestCase):
    """PinController.location_data_overview() - the aggregation endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_pin_owned_by_another_user_returns_404(self) -> None:
        other_pin: Pin = baker.make_recipe("dashboard.pin")
        response = self.client.get(reverse("pin.location_data_overview", args=[other_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_no_coordinates_returns_204(self) -> None:
        location: Location = baker.make("dashboard.Location", latitude=0, longitude=0)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        response = self.client.get(reverse("pin.location_data_overview", args=[pin.slug]))
        self.assertEqual(response.status_code, 204)

    def test_nothing_ready_schedules_every_source_and_returns_pending(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overview")
        scheduled_keys = {call.kwargs["args"][0] for call in fetch_task.apply_async.call_args_list}
        self.assertIn("nominatim", scheduled_keys)
        self.assertIn("photon", scheduled_keys)
        self.assertIn("overture_building_attributes", scheduled_keys)
        self.assertIn("open_elevation", scheduled_keys)

    def test_renders_ready_sources_combined(self) -> None:
        LocationCache.set(self.pin.location, "photon", {"name": "Ready Place", "osm_value": "cafe"}, query_key="")
        LocationCache.set(self.pin.location, "open_elevation", {"elevation_m": 100.0}, query_key="")
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready Place")
        self.assertContains(response, "above sea level")

    def test_partial_results_keep_self_polling(self) -> None:
        """Some sources ready, others still pending - render now, but keep polling."""
        LocationCache.set(self.pin.location, "photon", {"name": "Ready Place", "osm_value": "cafe"}, query_key="")
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready Place")
        self.assertContains(response, "hx-trigger")

    def test_all_sources_empty_and_settled_returns_204(self) -> None:
        """Every source fetched, none had anything useful - and nothing left pending."""
        for key in ("nominatim", "photon", "overture_building_attributes", "open_elevation"):
            LocationCache.set(self.pin.location, key, {}, query_key="")
        response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 204)
