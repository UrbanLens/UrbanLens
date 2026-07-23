"""Tests for the Location Data card's "Overview" tab.

Covers PinController.location_data_overview() (the aggregation endpoint) and
_location_data_overview_fields() (the per-source adapter that turns each
source's cached data into generic {label, value, href} facts, merged across
sources into one unattributed summary - see docs/prompts/completed.md).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.pin import PinController
from urbanlens.dashboard.models.cache.location_cache import LocationCache

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class LocationDataOverviewFieldsAdapterTests(SimpleTestCase):
    """_location_data_overview_fields() - pure data-shape adaptation, no DB needed."""

    def setUp(self) -> None:
        super().setUp()
        self.controller = PinController()

    def test_unknown_source_key_yields_none(self) -> None:
        self.assertIsNone(self.controller._location_data_overview_fields("not_a_real_source", {"name": "Test"}))

    def test_photon_with_no_name_yields_none(self) -> None:
        self.assertIsNone(self.controller._location_data_overview_fields("photon", {}))

    def test_photon_adapts_address_into_fields(self) -> None:
        piece = self.controller._location_data_overview_fields(
            "photon",
            {"name": "Test Place", "osm_value": "cafe", "housenumber": "10", "street": "Main St", "city": "Poughkeepsie", "osm_url": "https://openstreetmap.org/way/1"},
        )
        assert piece is not None
        self.assertEqual(piece["heading_name"], "Test Place")
        self.assertEqual(piece["chips"], ["Cafe"])
        self.assertIn({"label": "Street", "value": "10 Main St"}, piece["fields"])
        self.assertIn({"label": "City", "value": "Poughkeepsie"}, piece["fields"])
        self.assertEqual(piece["footer_link"], {"url": "https://openstreetmap.org/way/1", "label": "View raw OSM entry"})

    def test_nominatim_with_no_name_yields_none(self) -> None:
        self.assertIsNone(self.controller._location_data_overview_fields("nominatim", {}))

    def test_nominatim_adapts_place_dict_into_fields(self) -> None:
        place = {
            "name": "Test Cafe",
            "kind_label": "Cafe",
            "website": "https://example.test",
            "phone": "555-0100",
            "opening_hours": "Mo-Fr 08:00-18:00",
            "operator": "Test Operator",
            "osm_url": "https://openstreetmap.org/node/1",
        }
        piece = self.controller._location_data_overview_fields("nominatim", place)
        assert piece is not None
        self.assertEqual(piece["heading_name"], "Test Cafe")
        self.assertEqual(piece["chips"], ["Cafe"])
        self.assertIn({"label": "Website", "value": "https://example.test", "href": "https://example.test"}, piece["fields"])
        self.assertIn({"label": "Phone", "value": "555-0100", "href": "tel:555-0100"}, piece["fields"])
        self.assertEqual(piece["footer_link"]["url"], "https://openstreetmap.org/node/1")

    def test_nominatim_with_name_only_has_no_fields_or_footer(self) -> None:
        piece = self.controller._location_data_overview_fields("nominatim", {"name": "Bare Place"})
        assert piece is not None
        self.assertEqual(piece["fields"], [])
        self.assertIsNone(piece["footer_link"])

    def test_overture_building_attributes_adapts_into_fields(self) -> None:
        piece = self.controller._location_data_overview_fields(
            "overture_building_attributes",
            {"primary_name": "Test Hall", "subtype": "commercial", "height_m": 12.4, "num_floors": 3, "nearby_places": [{"name": "Corner Store", "category": "shop", "distance_m": 42.0}]},
        )
        assert piece is not None
        self.assertEqual(piece["heading_name"], "Test Hall")
        self.assertEqual(piece["chips"], ["Commercial"])
        self.assertIn({"label": "Height", "value": "12 m"}, piece["fields"])
        self.assertIn({"label": "Floors", "value": "3"}, piece["fields"])
        self.assertIn({"label": "Nearby", "value": "Corner Store - Shop (42m)"}, piece["fields"])

    def test_overture_building_attributes_empty_yields_none(self) -> None:
        self.assertIsNone(self.controller._location_data_overview_fields("overture_building_attributes", {}))

    def test_open_elevation_adapts_into_a_field(self) -> None:
        piece = self.controller._location_data_overview_fields("open_elevation", {"elevation_m": 58.0})
        assert piece is not None
        self.assertIsNone(piece["heading_name"])
        self.assertEqual(piece["chips"], [])
        self.assertEqual(len(piece["fields"]), 1)
        self.assertEqual(piece["fields"][0]["label"], "Elevation")
        self.assertIn("above sea level", piece["fields"][0]["value"])

    def test_open_elevation_missing_data_yields_none(self) -> None:
        self.assertIsNone(self.controller._location_data_overview_fields("open_elevation", {}))


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

    def test_renders_ready_sources_merged(self) -> None:
        LocationCache.set(self.pin.location, "photon", {"name": "Ready Place", "osm_value": "cafe"}, query_key="")
        LocationCache.set(self.pin.location, "open_elevation", {"elevation_m": 100.0}, query_key="")
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready Place")
        self.assertContains(response, "above sea level")

    def test_no_per_source_headers_or_titles_leak_into_the_merged_view(self) -> None:
        """The whole point of the merge: no "Photon"/"Building Characteristics" headers."""
        LocationCache.set(self.pin.location, "photon", {"name": "Ready Place", "osm_value": "cafe", "city": "Testville"}, query_key="")
        LocationCache.set(
            self.pin.location,
            "overture_building_attributes",
            {"primary_name": "Ready Place", "subtype": "residential", "height_m": 10.0},
            query_key="",
        )
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        # The place name is merged to a single heading, not repeated per-source.
        self.assertEqual(response.content.decode().count("Ready Place"), 1)
        self.assertNotContains(response, "Photon")
        self.assertNotContains(response, "Building Characteristics")

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

    def test_all_sources_empty_notifies_the_client_to_hide_every_tab(self) -> None:
        for key in ("nominatim", "photon", "overture_building_attributes", "open_elevation"):
            LocationCache.set(self.pin.location, key, {}, query_key="")
        response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(set(trigger["pinLocationDataEmpty"]["keys"]), {"nominatim", "photon", "overture_building_attributes", "open_elevation"})

    def test_a_settled_but_empty_source_is_flagged_even_when_others_are_ready(self) -> None:
        """Photon has real data; nominatim settled with nothing - only nominatim should be flagged."""
        LocationCache.set(self.pin.location, "photon", {"name": "Ready Place", "osm_value": "cafe"}, query_key="")
        LocationCache.set(self.pin.location, "nominatim", {}, query_key="")
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["pinLocationDataEmpty"]["keys"], ["nominatim"])

    def test_a_settled_but_empty_source_is_flagged_while_others_are_still_pending(self) -> None:
        """Nominatim settled with nothing; photon/others not yet fetched (still pending)."""
        LocationCache.set(self.pin.location, "nominatim", {}, query_key="")
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hx-trigger")  # still self-polling for the pending sources
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["pinLocationDataEmpty"]["keys"], ["nominatim"])

    def test_nothing_settled_yet_carries_no_empty_tab_notification(self) -> None:
        """Nothing ready at all - every source just got scheduled, none confirmed empty yet."""
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Trigger", response)
