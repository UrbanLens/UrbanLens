"""The "child pin details" toggle surfaces a property's building child pins on the property's own page."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.plugins.registry import PluginRegistry
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, PanelPlacement, panel_sources

if TYPE_CHECKING:
    from django.http import HttpResponse

BUILDING_LEVEL_KEYS = (
    "cris_building",
    "redata_building_attributes",
    "overture_building_attributes",
    "redata_historic_registers",
)
AREA_LEVEL_KEYS = ("photon", "open_elevation", "census_tigerweb", "hazard_history", "gdelt")

_CARD = 'class="child-building-detail"'


class _FutureBuildingPanelSource(InfoPanelSource):
    """A building-level panel a plugin could ship, which the building card picks up by declaration alone."""

    key = "future_building_panel"
    cache_source = "future_building_panel"
    section_id = "future-building-panel-section"
    icon = "science"
    title = "Future Building Panel"
    placement: ClassVar[PanelPlacement] = PanelPlacement.STANDALONE
    building_level: ClassVar[bool] = True

    def fetch(self, pin: Pin) -> None:
        """Never called here."""

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        return {"chips": [data["value"]]} if data.get("value") else None


def _with_future_panel():
    original = PluginRegistry.panel_sources

    def sources(registry: PluginRegistry) -> list:
        return [*original(registry), _FutureBuildingPanelSource()]

    return mock.patch.object(PluginRegistry, "panel_sources", sources)


def _building(parent: Pin, name: str, **kwargs) -> Pin:
    return baker.make_recipe(
        "dashboard.pin",
        profile=parent.profile,
        parent_pin=parent,
        pin_type=PinType.BUILDING,
        name=name,
        **kwargs,
    )


def _near(pin: Pin, metres: float) -> Location:
    """A location ``metres`` north of a pin."""
    return baker.make(
        Location,
        latitude=pin.location.latitude + Decimal(str(round(metres / 111_320, 7))),
        longitude=pin.location.longitude,
    )


class BuildingLevelDeclarationTests(SimpleTestCase):
    """Panels that describe one structure say so; panels about the area do not."""

    def test_building_panels_declare_it(self) -> None:
        sources = panel_sources()
        self.assertEqual(
            [key for key in BUILDING_LEVEL_KEYS if not getattr(sources[key], "building_level", False)],
            [],
        )

    def test_area_panels_do_not(self) -> None:
        sources = panel_sources()
        self.assertEqual([key for key in AREA_LEVEL_KEYS if getattr(sources[key], "building_level", False)], [])


class _Base(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.parent: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile, name="Maple Street House")

    def _page(self, query: str = "") -> HttpResponse:
        response = self.client.get(reverse("pin.details", args=[self.parent.slug]) + query)
        self.assertEqual(response.status_code, 200)
        return response

    def _card(self, building: Pin) -> HttpResponse:
        return self.client.get(reverse("pin.child_building", args=[building.slug]))

    def _buildings_list(self) -> str:
        response = self.client.get(reverse("pin.parcel_buildings", args=[self.parent.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class BuildingCardTests(_Base):
    """A building child's card, opened in place from the property's Buildings list (P172)."""

    def setUp(self) -> None:
        super().setUp()
        LocationCache.set(self.parent.location, PARCEL_BUILDINGS_CACHE_SOURCE, {})
        self.building = _building(self.parent, "Carriage House", description="Slate roof, rear wall collapsed.")

    def test_the_toggle_starts_on(self) -> None:
        response = self._page()
        self.assertTrue(response.context["include_children"])
        self.assertContains(response, reverse("pin.parcel_buildings", args=[self.parent.slug]) + "?children=1")

    def test_turning_it_off_is_remembered_on_the_page(self) -> None:
        self.assertFalse(self._page("?children=0").context["include_children"])

    def test_the_buildings_row_opens_the_card_in_place_without_loading_it_first(self) -> None:
        content = self._buildings_list()
        self.assertIn(f'hx-get="{reverse("pin.child_building", args=[self.building.slug])}"', content)
        self.assertIn('hx-trigger="toggle once"', content)
        self.assertNotIn(_CARD, content)
        self.assertNotIn("/building-panel/", content)

    def test_the_card_names_and_links_the_building(self) -> None:
        response = self._card(self.building)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _CARD)
        self.assertContains(response, "Slate roof, rear wall collapsed.")
        self.assertContains(response, reverse("pin.details", args=[self.building.slug]))

    def test_the_card_loads_the_buildings_own_building_panels_only(self) -> None:
        content = self._card(self.building).content.decode()
        for key in BUILDING_LEVEL_KEYS:
            self.assertIn(reverse("pin.building_panel", args=[self.building.slug, key]), content)
        for key in AREA_LEVEL_KEYS:
            self.assertNotIn(f"/{key}/", content)

    def test_a_plugin_building_panel_joins_the_card_by_declaration(self) -> None:
        with _with_future_panel():
            content = self._card(self.building).content.decode()
        self.assertIn(reverse("pin.building_panel", args=[self.building.slug, "future_building_panel"]), content)

    def test_a_building_panel_renders_nested_under_an_id_of_its_own(self) -> None:
        LocationCache.set(self.building.location, "future_building_panel", {"value": "Brick"}, query_key="")
        with _with_future_panel():
            response = self.client.get(
                reverse("pin.building_panel", args=[self.building.slug, "future_building_panel"])
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="simple-info-panel nested"')
        self.assertContains(response, f'id="future-building-panel-section--{self.building.slug}"')
        self.assertContains(response, "Brick")

    def test_a_pending_building_panel_polls_under_the_same_id(self) -> None:
        with (
            _with_future_panel(),
            mock.patch("urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch", return_value=True),
        ):
            response = self.client.get(
                reverse("pin.building_panel", args=[self.building.slug, "future_building_panel"])
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="future-building-panel-section--{self.building.slug}"')
        self.assertContains(response, reverse("pin.building_panel", args=[self.building.slug, "future_building_panel"]))

    def test_an_area_panel_is_not_served_as_a_building_panel(self) -> None:
        response = self.client.get(reverse("pin.building_panel", args=[self.building.slug, "photon"]))
        self.assertEqual(response.status_code, 404)

    def test_another_users_building_is_not_found(self) -> None:
        self.client.force_login(baker.make(User))
        self.assertEqual(self._card(self.building).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("pin.parcel_buildings", args=[self.parent.slug])).status_code,
            404,
        )


class ToggleDefaultTests(_Base):
    """When the toggle starts on without being asked."""

    def test_a_pin_with_only_non_building_children_starts_off(self) -> None:
        baker.make_recipe("dashboard.pin", profile=self.user.profile, parent_pin=self.parent, pin_type=PinType.ENTRANCE)
        self.assertFalse(self._page().context["include_children"])

    def test_a_building_with_one_building_inside_starts_off(self) -> None:
        Pin.objects.filter(pk=self.parent.pk).update(pin_type=PinType.BUILDING, pin_type_is_user_provided=True)
        _building(self.parent, "Chapel")
        self.assertFalse(self._page().context["include_children"])

    def test_a_property_classified_as_a_building_by_its_footprint_still_starts_on(self) -> None:
        Pin.objects.filter(pk=self.parent.pk).update(pin_type=PinType.BUILDING, pin_type_is_user_provided=False)
        _building(self.parent, "House")
        self.assertTrue(self._page().context["include_children"])
