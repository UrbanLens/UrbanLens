"""Tests for the hex_to_rgba template filter (color+opacity swatch tints)."""
from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.templatetags.dashboard_tags import hex_to_rgba


class HexToRgbaTests(SimpleTestCase):
    def test_converts_hex_and_percent_opacity_to_rgba(self) -> None:
        self.assertEqual(hex_to_rgba("#F44336", 50), "rgba(244,67,54,0.5)")

    def test_defaults_to_fully_opaque(self) -> None:
        self.assertEqual(hex_to_rgba("#000000"), "rgba(0,0,0,1.0)")

    def test_blank_hex_returns_empty_string(self) -> None:
        self.assertEqual(hex_to_rgba(""), "")
        self.assertEqual(hex_to_rgba(None), "")

    def test_invalid_hex_returns_empty_string(self) -> None:
        self.assertEqual(hex_to_rgba("javascript:alert(1)", 50), "")
        self.assertEqual(hex_to_rgba("#ZZZZZZ", 50), "")

    def test_opacity_is_clamped_to_0_100(self) -> None:
        self.assertEqual(hex_to_rgba("#FFFFFF", 500), "rgba(255,255,255,1.0)")
        self.assertEqual(hex_to_rgba("#FFFFFF", -50), "rgba(255,255,255,0.0)")

    def test_unparseable_opacity_falls_back_to_fully_opaque(self) -> None:
        self.assertEqual(hex_to_rgba("#FFFFFF", "not-a-number"), "rgba(255,255,255,1.0)")
