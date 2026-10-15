"""What a plugin's map layers and overlay sources look like on the pages that offer them."""

from __future__ import annotations

import json
from typing import ClassVar

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.points import PIN_MAP_LAYERS
from urbanlens.dashboard.services.integration_testing.accounts import prepare_signed_in_account
from urbanlens.dashboard.services.map.pin_layers import PinMapLayer, offered_pin_layers, pin_layers_config
from urbanlens.dashboard.templatetags.map_components import MapLayerSpec
from urbanlens.dashboard.tests.hypothesis.overlay_source_fake import fake_sheet_source


class _ZonesLayer(PinMapLayer):
    spec: ClassVar[MapLayerSpec] = MapLayerSpec(
        key="zones",
        kind="custom",
        label="Zones",
        aria_label="Show or hide zones",
        tooltip="Zones around this pin",
        icon="fence",
        button_id="zones-button",
    )
    attribution: ClassVar[str] = "Zones: Example Agency"

    def __init__(self, *, offered: bool = True) -> None:
        self._offered = offered

    def offered(self, pin: Pin) -> bool:
        return self._offered

    def data_url(self, pin: Pin) -> str:
        return f"/dashboard/map/pin/{pin.slug}/zones/json/"


class _PageCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = prepare_signed_in_account(self.user)
        self.location = baker.make(Location, latitude=35.14, longitude=-79.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Mill")
        self.client.force_login(self.user)
        self.pin_page = reverse("pin.details", kwargs={"pin_slug": self.pin.ensure_slug()})


class PinMapLayerTests(_PageCase):
    def test_only_the_layers_that_say_they_are_offered_are_offered(self) -> None:
        shown, hidden = _ZonesLayer(), _ZonesLayer(offered=False)

        with PIN_MAP_LAYERS.overridden([hidden, shown]):
            self.assertEqual(offered_pin_layers(self.pin), [shown])

    def test_the_page_script_is_told_each_layers_key_endpoint_and_credit(self) -> None:
        layer = _ZonesLayer()

        self.assertEqual(
            pin_layers_config(self.pin, [layer]),
            [{"key": "zones", "url": layer.data_url(self.pin), "attribution": "Zones: Example Agency"}],
        )

    def test_the_pin_page_offers_the_layers_button_and_embeds_its_endpoint(self) -> None:
        with PIN_MAP_LAYERS.overridden([_ZonesLayer()]):
            response = self.client.get(self.pin_page)

        self.assertContains(response, 'data-map-layer="zones"')
        self.assertContains(response, "data-feature-layers=")
        embedded = response.context["feature_layers_json"]
        self.assertEqual([entry["key"] for entry in json.loads(embedded)], ["zones"])

    def test_a_layer_that_is_not_offered_leaves_no_button_and_no_endpoint(self) -> None:
        with PIN_MAP_LAYERS.overridden([_ZonesLayer(offered=False)]):
            response = self.client.get(self.pin_page)

        self.assertNotContains(response, 'data-map-layer="zones"')
        self.assertEqual(json.loads(response.context["feature_layers_json"]), [])


class OverlaySourceListTests(_PageCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(Wiki, location=self.location)
        self.wiki_page = reverse("location.wiki", args=[self.location.slug])

    def test_the_pin_page_lists_each_available_source_in_the_overlay_manager(self) -> None:
        with fake_sheet_source():
            response = self.client.get(self.pin_page)

        self.assertContains(response, "Fake sheets covering this location")
        self.assertContains(response, 'hx-get="/dashboard/fake-sheets/"')

    def test_the_wiki_page_lists_them_too(self) -> None:
        with fake_sheet_source():
            response = self.client.get(self.wiki_page)

        self.assertContains(response, "Fake sheets covering this location")

    def test_with_no_source_the_manager_has_no_list(self) -> None:
        with fake_sheet_source(none=True):
            response = self.client.get(self.pin_page)

        self.assertNotContains(response, "Fake sheets covering this location")
        self.assertNotContains(response, "map-overlay-historical-heading")
