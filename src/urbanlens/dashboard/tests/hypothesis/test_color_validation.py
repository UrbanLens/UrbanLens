"""Colour values must be validated on the way in, not only on the way out.

`Label.color` declares `choices` and `MarkupShape.color`/`border_color` declare nothing,
and Django enforces field `choices` only in `full_clean()` - which `save()` does not call.
Every write path assigned straight from request data, so a value like
`x" onmouseover="alert(1)` stored cleanly and was later interpolated into a `style="…"`
attribute by the map and label renderers.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.colors import NO_COLOR, clean_color


class CleanColorTests(SimpleTestCase):
    def test_palette_colours_pass_through(self) -> None:
        for value in ("#F44336", "#2196F3", "#abc", "#ABCDEF"):
            self.assertEqual(clean_color(value), value)

    def test_attribute_breakout_is_rejected(self) -> None:
        self.assertIsNone(clean_color('x" onmouseover="alert(1)'))
        self.assertIsNone(clean_color('#fff" onload="alert(1)'))

    def test_css_injection_is_rejected(self) -> None:
        self.assertIsNone(clean_color("url(https://example.com/x)"))
        self.assertIsNone(clean_color("red;background:url(x)"))

    def test_a_rejected_value_becomes_the_caller_s_default(self) -> None:
        self.assertEqual(clean_color("javascript:alert(1)", default="#e53e3e"), "#e53e3e")
        self.assertEqual(clean_color(None, default=""), "")

    def test_the_none_keyword_is_only_allowed_where_it_means_something(self) -> None:
        """Markup borders use "none" to mean "no border"; nothing else should accept
        a bare CSS keyword."""
        self.assertEqual(clean_color("none", allow_none_keyword=True), NO_COLOR)
        self.assertIsNone(clean_color("none"))

    @given(st.text(max_size=40))
    def test_output_is_always_a_colour_a_default_or_none(self, value: str) -> None:
        """The property that actually matters: whatever is thrown at it, the result is
        never an arbitrary string that could reach a style attribute."""
        result = clean_color(value, allow_none_keyword=True)

        if result is not None and result != NO_COLOR:
            self.assertRegex(result, r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
