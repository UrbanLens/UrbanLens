"""Where the Private Pin page puts each info panel: a card of its own, or a tab in Regional Data or Location Data."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, ClassVar
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.plugins.registry import PluginRegistry
from urbanlens.dashboard.services.pins.external_data import (
    InfoPanelSource,
    OverviewSummary,
    PanelPlacement,
    panel_sources,
    tabbed_panels,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

REGIONAL_KEYS = (
    "census_tigerweb",
    "inaturalist",
    "usgs_earthquakes",
    "hazard_history",
    "redata_hydrology",
    "redata_air_quality",
    "epa_echo",
)
LOCATION_KEYS = ("photon", "overture_building_attributes", "open_elevation", "redata_historic_registers")
MOVED_TO_REGIONAL = ("hazard_history", "redata_hydrology", "redata_air_quality")


def _info_panel(key: str) -> InfoPanelSource:
    source = panel_sources()[key]
    assert isinstance(source, InfoPanelSource), f"{key} is not an info panel"
    return source


class _FuturePanelSource(InfoPanelSource):
    """A panel a third-party plugin could ship, choosing its placement by declaration alone."""

    key = "future_regional_panel"
    cache_source = "future_regional_panel"
    section_id = "future-regional-panel-section"
    icon = "science"
    title = "Future Regional Panel"
    placement: ClassVar[PanelPlacement] = PanelPlacement.REGIONAL
    tab_label: ClassVar[str] = "Future"

    def fetch(self, pin: Pin) -> None:
        """Never called here."""

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Never called here."""
        return None


class _FutureLocationPanelSource(InfoPanelSource):
    """A Location Data tab that adds nothing to the Overview."""

    key = "future_location_panel"
    cache_source = "future_location_panel"
    section_id = "future-location-panel-section"
    icon = "science"
    title = "Future Location Panel"
    placement: ClassVar[PanelPlacement] = PanelPlacement.LOCATION

    def fetch(self, pin: Pin) -> None:
        """Never called here."""

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Something to show whenever there is a row with a value."""
        return {"chips": [data["value"]]} if data.get("value") else None


class PlacementDeclarationTests(SimpleTestCase):
    """Each source declares its own placement; the controller holds no list of keys."""

    def test_regional_sources_declare_regional(self) -> None:
        wrong = {
            key: _info_panel(key).placement
            for key in REGIONAL_KEYS
            if _info_panel(key).placement != PanelPlacement.REGIONAL
        }
        self.assertEqual(wrong, {})

    def test_location_sources_declare_location(self) -> None:
        wrong = {
            key: _info_panel(key).placement
            for key in LOCATION_KEYS
            if _info_panel(key).placement != PanelPlacement.LOCATION
        }
        self.assertEqual(wrong, {})

    def test_the_default_is_a_card_of_its_own(self) -> None:
        self.assertEqual(_info_panel("gdelt").placement, PanelPlacement.STANDALONE)
        self.assertEqual(_info_panel("epa_echo_detail").placement, PanelPlacement.STANDALONE)

    def test_tabbed_panels_orders_by_tab_order_and_keeps_registry_order_on_ties(self) -> None:
        tabs = [source.key for source in tabbed_panels(panel_sources().values(), PanelPlacement.REGIONAL)]
        self.assertEqual(tabs, list(REGIONAL_KEYS))

    def test_tab_label_falls_back_to_the_title(self) -> None:
        source = _FuturePanelSource()
        self.assertEqual(source.label, "Future")
        with mock.patch.object(_FuturePanelSource, "tab_label", ""):
            self.assertEqual(source.label, "Future Regional Panel")


class PinPagePlacementTests(TestCase):
    """The page renders what the declarations say."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)

    def _page(self):
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_moved_sources_are_regional_data_tabs(self) -> None:
        tabs = [tab["key"] for tab in self._page().context["panel_tabs"]]
        for key in MOVED_TO_REGIONAL:
            self.assertIn(key, tabs)

    def test_historic_registers_is_a_location_data_tab(self) -> None:
        tabs = [tab["key"] for tab in self._page().context["location_data_tabs"]]
        self.assertIn("redata_historic_registers", tabs)

    def test_no_tabbed_source_is_also_a_standalone_card(self) -> None:
        standalone = {panel.key for panel in self._page().context["simple_info_panels"]}
        self.assertEqual(standalone & {*REGIONAL_KEYS, *LOCATION_KEYS}, set())

    def test_tabs_render_inside_their_cards_and_not_as_cards(self) -> None:
        content = self._page().content.decode()
        regional = content.split('id="pin-plugin-tabs-section"')[1].split('id="pin-plugin-tab-body"')[0]
        location = content.split('id="location-data-section"')[1].split('id="location-data-body"')[0]
        for key in MOVED_TO_REGIONAL:
            self.assertIn(reverse("pin.panel", args=[self.pin.slug, key]), regional)
        self.assertIn(reverse("pin.panel", args=[self.pin.slug, "redata_historic_registers"]), location)
        for section_id in (
            "hazard-history-section",
            "hydrology-section",
            "air-quality-section",
            "historic-registers-section",
        ):
            self.assertNotIn(f'id="{section_id}"', content, f"{section_id} still renders as a card of its own")

    def test_tab_labels(self) -> None:
        response = self._page()
        labels = {
            tab["key"]: tab["label"] for tab in response.context["panel_tabs"] + response.context["location_data_tabs"]
        }
        self.assertEqual(labels["hazard_history"], "Disasters")
        self.assertEqual(labels["redata_hydrology"], "Water")
        self.assertEqual(labels["redata_air_quality"], "Air Quality")
        self.assertEqual(labels["redata_historic_registers"], "Historic Registers")

    def test_a_plugin_can_place_a_new_panel_by_declaration(self) -> None:
        original = PluginRegistry.panel_sources

        def with_future_panel(registry: PluginRegistry) -> list:
            return [*original(registry), _FuturePanelSource()]

        with mock.patch.object(PluginRegistry, "panel_sources", with_future_panel):
            response = self._page()

        self.assertIn("future_regional_panel", [tab["key"] for tab in response.context["panel_tabs"]])
        self.assertNotIn("future_regional_panel", [panel.key for panel in response.context["simple_info_panels"]])
        self.assertContains(response, ">Future<")


class TabbedPanelChromeTests(TestCase):
    """A panel rendered into a tab body leaves the card to the strip."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)

    def test_moved_panels_render_nested(self) -> None:
        LocationCache.set(
            self.pin.location,
            "hazard_history",
            {
                "events": [
                    {
                        "provider": "fema_disasters",
                        "event_type": "severe_weather",
                        "occurred_at": "2023-07-09T00:00:00Z",
                        "attributes": {"designated_area": "Dutchess (County)"},
                    }
                ]
            },
            query_key="",
        )
        with mock.patch("urbanlens.dashboard.plugins.builtin.hazard_history.redata_configured", return_value=True):
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "hazard_history"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="simple-info-panel nested"')
        self.assertContains(response, "1 federal disaster declaration for this county")


class HistoricRegisterOverviewSummaryTests(SimpleTestCase):
    """What the historic-registers source adds to Location Data's Overview."""

    def _summary(self, *resources: dict, place_name: str = "") -> OverviewSummary | None:
        pin = mock.Mock(location=mock.Mock(official_name=place_name))
        return _info_panel("redata_historic_registers").overview_summary(pin, {"resources": list(resources)})

    def _notes(self, *resources: dict, place_name: str = "") -> list[str]:
        summary = self._summary(*resources, place_name=place_name)
        assert summary is not None, "no Overview summary"
        return summary.notes

    def test_a_national_register_listing_is_mentioned_by_name(self) -> None:
        notes = self._notes(
            {
                "provider": "nps_nrhp",
                "name": "Hudson River State Hospital, Main Building",
                "status": "Listed",
                "scope": "structure",
            }
        )
        self.assertEqual(
            notes,
            ["Listed on the National Register of Historic Places as “Hudson River State Hospital, Main Building”"],
        )

    def test_a_status_other_than_listed_is_named(self) -> None:
        notes = self._notes(
            {"provider": "nps_nrhp", "name": "Old Mill", "status": "Determined Eligible", "scope": "site"}
        )
        self.assertEqual(notes, ["On the National Register of Historic Places as “Old Mill” (Determined Eligible)"])

    def test_a_site_record_is_preferred_over_a_structure(self) -> None:
        notes = self._notes(
            {"provider": "nps_nrhp", "name": "Main Building", "status": "Listed", "scope": "structure"},
            {"provider": "nps_nrhp", "name": "The Campus", "status": "Listed", "scope": "site"},
        )
        self.assertEqual(
            notes, ["Listed on the National Register of Historic Places as “The Campus”, with 1 other listing nearby"]
        )

    def test_the_listing_named_like_the_place_beats_a_nearer_neighbour(self) -> None:
        """Measured on HRSH: REData returns the Isaac Roosevelt House first, nearer the pin than the hospital."""
        notes = self._notes(
            {"provider": "nps_nrhp", "name": "Roosevelt, Isaac, House", "status": "Listed", "scope": "structure"},
            {
                "provider": "nps_nrhp",
                "name": "Hudson River State Hospital, Main Building",
                "status": "Listed",
                "scope": "structure",
            },
            place_name="Hudson River State Hospital",
        )
        self.assertIn("“Hudson River State Hospital, Main Building”", notes[0])

    def test_with_no_name_to_match_the_nearest_listing_is_named(self) -> None:
        notes = self._notes(
            {"provider": "nps_nrhp", "name": "Roosevelt, Isaac, House", "status": "Listed", "scope": "structure"},
            {
                "provider": "nps_nrhp",
                "name": "Hudson River State Hospital, Main Building",
                "status": "Listed",
                "scope": "structure",
            },
        )
        self.assertIn("“Roosevelt, Isaac, House”", notes[0])

    def test_the_listing_whose_boundary_holds_the_pin_wins(self) -> None:
        notes = self._notes(
            {
                "provider": "nps_nrhp",
                "name": "The Campus",
                "status": "Listed",
                "scope": "site",
                "contains_point": False,
            },
            {
                "provider": "nps_nrhp",
                "name": "Main Building",
                "status": "Listed",
                "scope": "structure",
                "contains_point": True,
            },
            place_name="The Campus",
        )
        self.assertIn("“Main Building”", notes[0])

    def test_a_listing_known_not_to_hold_the_pin_is_only_named_as_nearby(self) -> None:
        notes = self._notes(
            {
                "provider": "nps_nrhp",
                "name": "Roosevelt, Isaac, House",
                "status": "Listed",
                "scope": "structure",
                "contains_point": False,
            },
            place_name="Hudson River State Hospital",
        )
        self.assertEqual(
            notes, ["The nearest listing on the National Register of Historic Places is “Roosevelt, Isaac, House”"]
        )

    def test_only_other_registers_says_nothing(self) -> None:
        self.assertIsNone(
            self._summary({"provider": "md_mihp", "name": "Some House", "status": "Listed", "scope": "structure"})
        )

    def test_no_rows_says_nothing(self) -> None:
        self.assertIsNone(self._summary())


class HistoricRegisterOverviewEndpointTests(TestCase):
    """The Overview tab's rendering of the listing, and its effect on the Historic Registers tab."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        for key in ("nominatim", "photon", "overture_building_attributes", "open_elevation"):
            LocationCache.set(self.pin.location, key, {}, query_key="")

    def test_a_tab_with_nothing_for_the_overview_is_kept_while_it_has_content(self) -> None:
        """Adding nothing to the Overview is the default, so it cannot be what hides a tab."""
        LocationCache.set(self.pin.location, "redata_historic_registers", {"resources": []}, query_key="")
        LocationCache.set(self.pin.location, "future_location_panel", {"value": "something"}, query_key="")
        original = PluginRegistry.panel_sources

        def with_future_panel(registry: PluginRegistry) -> list:
            return [*original(registry), _FutureLocationPanelSource()]

        with mock.patch.object(PluginRegistry, "panel_sources", with_future_panel):
            hidden = self._hidden_tabs(self._overview())

        self.assertNotIn("future_location_panel", hidden)

    def _overview(self):
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            return self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))

    def _hidden_tabs(self, response) -> list[str]:
        if "HX-Trigger" not in response:
            return []
        return json.loads(response["HX-Trigger"])["pinLocationDataEmpty"]["keys"]

    def test_a_listing_is_mentioned_and_links_to_its_tab(self) -> None:
        LocationCache.set(
            self.pin.location,
            "redata_historic_registers",
            {
                "resources": [
                    {
                        "provider": "nps_nrhp",
                        "name": "Hudson River State Hospital, Main Building",
                        "status": "Listed",
                        "scope": "structure",
                    }
                ]
            },
            query_key="",
        )
        response = self._overview()

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Listed on the National Register of Historic Places as “Hudson River State Hospital, Main Building”",
        )
        button = re.search(r"<button[^>]*data-open-tab=\"redata_historic_registers\"[^>]*>", response.content.decode())
        self.assertIsNotNone(button, "the mention does not link to the Historic Registers tab")
        self.assertNotIn("redata_historic_registers", self._hidden_tabs(response))

    def test_no_listing_no_mention(self) -> None:
        LocationCache.set(
            self.pin.location,
            "redata_historic_registers",
            {"resources": [{"provider": "md_mihp", "name": "Some House", "status": "", "scope": "structure"}]},
            query_key="",
        )
        response = self._overview()

        self.assertNotContains(response, "National Register", status_code=response.status_code)
        self.assertNotIn(
            "redata_historic_registers", self._hidden_tabs(response), "a state-register row is still worth its tab"
        )

    def test_an_empty_register_answer_hides_the_tab(self) -> None:
        LocationCache.set(self.pin.location, "redata_historic_registers", {"resources": []}, query_key="")
        response = self._overview()

        self.assertIn("redata_historic_registers", self._hidden_tabs(response))
