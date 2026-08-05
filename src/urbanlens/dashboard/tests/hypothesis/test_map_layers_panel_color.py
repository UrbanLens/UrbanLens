"""Tests that a CustomLayer's color survives into the rendered layers panel.

Regression coverage for a bug where the color was captured, stored, and
serialized correctly but never actually rendered anywhere the layer's
button/thumbnail appeared - see custom_layer_button() and _layers_panel.html.
"""
from __future__ import annotations

from django.template.loader import render_to_string
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.templatetags.map_components import custom_layer_button, map_layers_panel


class CustomLayerButtonColorTests(SimpleTestCase):
    def test_carries_the_layers_color(self) -> None:
        layer = baker.prepare("dashboard.CustomLayer", name="Tunnels", color="#F44336", icon="route")
        self.assertEqual(custom_layer_button(layer).color, "#F44336")

    def test_blank_color_stays_blank(self) -> None:
        layer = baker.prepare("dashboard.CustomLayer", name="Tunnels", color="", icon="route")
        self.assertEqual(custom_layer_button(layer).color, "")


class MapLayersPanelColorRenderTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def test_colored_custom_layer_tints_its_thumbnail(self) -> None:
        layer = baker.make("dashboard.CustomLayer", name="Tunnels", color="#F44336", icon="route", parent_pin=self.pin, profile=self.profile)
        context = map_layers_panel(layers="", custom_layers=[layer])
        html = render_to_string("dashboard/partials/map/_layers_panel.html", context)
        self.assertIn("background:rgba(244,67,54,0.18)", html)

    def test_colorless_custom_layer_has_no_inline_background(self) -> None:
        layer = baker.make("dashboard.CustomLayer", name="Tunnels", color="", icon="route", parent_pin=self.pin, profile=self.profile)
        context = map_layers_panel(layers="", custom_layers=[layer])
        html = render_to_string("dashboard/partials/map/_layers_panel.html", context)
        self.assertNotIn("background:rgba", html)
