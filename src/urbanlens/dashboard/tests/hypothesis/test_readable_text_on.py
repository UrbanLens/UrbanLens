"""The readable_text_on filter: chip text that reads on a user-chosen colour over the dark filter panel (P51)."""

from __future__ import annotations

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.templatetags.dashboard_tags import READABLE_DARK_TEXT, READABLE_LIGHT_TEXT, readable_text_on

_PANEL = (14, 16, 22)


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _hex(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


class ReadableTextOnTests(SimpleTestCase):
    def test_a_mid_blue_takes_dark_text(self) -> None:
        self.assertEqual(readable_text_on("#2196F3", 100), READABLE_DARK_TEXT)

    def test_a_pale_yellow_takes_dark_text(self) -> None:
        self.assertEqual(readable_text_on("#FFEB3B", 100), READABLE_DARK_TEXT)

    def test_a_deep_colour_takes_light_text(self) -> None:
        self.assertEqual(readable_text_on("#1A237E", 100), READABLE_LIGHT_TEXT)

    def test_a_faint_tint_over_the_dark_panel_takes_light_text(self) -> None:
        self.assertEqual(readable_text_on("#FFEB3B", 10), READABLE_LIGHT_TEXT)

    def test_no_or_invalid_colour_leaves_the_text_alone(self) -> None:
        for value in (None, "", "#ZZZZZZ", "red", "javascript:alert(1)"):
            self.assertEqual(readable_text_on(value, 100), "")

    @given(
        st.integers(0, 0xFFFFFF).map(lambda n: f"#{n:06X}"),
        st.integers(0, 100),
    )
    def test_the_choice_is_the_more_legible_of_the_two(self, colour: str, opacity: int) -> None:
        alpha = opacity / 100
        backdrop = tuple(c * alpha + p * (1 - alpha) for c, p in zip(_hex(colour), _PANEL, strict=True))
        chosen = readable_text_on(colour, opacity)
        other = READABLE_LIGHT_TEXT if chosen == READABLE_DARK_TEXT else READABLE_DARK_TEXT
        self.assertGreaterEqual(_contrast(_hex(chosen), backdrop), _contrast(_hex(other), backdrop) - 1e-9)
