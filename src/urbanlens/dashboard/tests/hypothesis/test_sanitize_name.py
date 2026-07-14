"""Tests for sanitize_name - strict-charset sanitization of user-facing names."""

from __future__ import annotations

import unicodedata

from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.locations.naming import sanitize_name

_hyp = hyp_settings(max_examples=100, deadline=None)

_UNCHANGED_EXAMPLES = (
    "Riverside Mill",
    "St. Mark's Church",
    "O'Brien's Bar & Grill",
    "Route 9/20",
    "Building #3",
    "Café Müller",
    "Château Frontenac",
    "東京タワー",
    "Москва",
    "Al-Masjid",
    "Kilómetro 5",
)


class SanitizeNameTests(TestCase):
    """sanitize_name allowlists letters/digits/space plus everyday punctuation."""

    def test_none_and_empty_pass_through_unchanged(self) -> None:
        self.assertIsNone(sanitize_name(None))
        self.assertEqual(sanitize_name(""), "")

    def test_ordinary_names_are_unchanged(self) -> None:
        for name in _UNCHANGED_EXAMPLES:
            with self.subTest(name=name):
                self.assertEqual(sanitize_name(name), name)

    def test_angle_brackets_are_stripped(self) -> None:
        result = sanitize_name("<script>alert(1)</script>")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)

    def test_curly_quotes_and_dashes_are_normalized(self) -> None:
        # left/right single quotes, left/right double quotes, em dash.
        raw = "Curly ‘quotes’ and “dashes—here”"
        result = sanitize_name(raw)
        self.assertEqual(result, "Curly 'quotes' and \"dashes-here\"")

    def test_control_and_zero_width_characters_are_dropped(self) -> None:
        # "Evil" + zero-width space (U+200B) + "Name" + " " + "Here" + RTL
        # override (U+202E), built via chr() rather than a literal escape so
        # the source file itself doesn't embed a raw bidi-control character
        # (ruff PLE2502).
        raw = "Evil" + chr(0x200B) + "Name Here" + chr(0x202E)
        result = sanitize_name(raw)
        self.assertEqual(result, "EvilName Here")

    def test_emoji_and_symbols_are_dropped(self) -> None:
        result = sanitize_name("Cool Spot \U0001f525\U0001f4a5 ~ * ^")
        self.assertEqual(result, "Cool Spot")

    def test_whitespace_runs_are_collapsed_and_trimmed(self) -> None:
        self.assertEqual(sanitize_name("  Riverside   Mill  "), "Riverside Mill")

    def test_semicolons_and_pipes_are_dropped(self) -> None:
        result = sanitize_name("Name; DROP | pipe")
        self.assertNotIn(";", result)
        self.assertNotIn("|", result)


class SanitizeNamePropertyTests(TestCase):
    """Property-based checks that hold for arbitrary input."""

    @given(text=st.text(min_size=0, max_size=200))
    @_hyp
    def test_never_produces_markup_significant_characters(self, text: str) -> None:
        result = sanitize_name(text)
        if not result:
            return
        for forbidden in ("<", ">", "`", "{", "}", "\\", "|", ";"):
            self.assertNotIn(forbidden, result)

    @given(text=st.text(min_size=0, max_size=200))
    @_hyp
    def test_never_produces_control_or_format_characters(self, text: str) -> None:
        result = sanitize_name(text)
        if not result:
            return
        for char in result:
            if char == " ":
                continue
            self.assertNotEqual(unicodedata.category(char)[0], "C")

    @given(text=st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Lo", "Nd")), min_size=1, max_size=50))
    @_hyp
    def test_letters_and_digits_from_any_script_never_produce_empty_output(self, text: str) -> None:
        # NFKC normalization can change the exact codepoints (e.g. fullwidth ->
        # ASCII, ligatures -> multi-char), so this only asserts nothing is lost
        # outright - every character is a keeper category, so the result must
        # be non-empty whenever the input is.
        result = sanitize_name(text)
        self.assertTrue(result)

    @given(text=st.text(min_size=0, max_size=200))
    @_hyp
    def test_is_idempotent(self, text: str) -> None:
        once = sanitize_name(text)
        twice = sanitize_name(once)
        self.assertEqual(once, twice)


class ModelSaveSanitizesNameTests(TestCase):
    """sanitize_name is actually invoked from every relevant model's save()."""

    def setUp(self) -> None:
        from django.contrib.auth.models import User

        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_pin_save_sanitizes_name(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(Location, latitude=41.0, longitude=-75.0)
        pin = baker.make(Pin, profile=self.profile, location=location, name="<script>Evil</script>")
        pin.refresh_from_db()
        self.assertNotIn("<", pin.name)
        self.assertNotIn(">", pin.name)

    def test_location_save_sanitizes_official_name(self) -> None:
        from urbanlens.dashboard.models.location.model import Location

        location = baker.make(Location, latitude=41.1, longitude=-75.1, official_name="<b>Bold</b> Place")
        location.refresh_from_db()
        self.assertNotIn("<", location.official_name)
        self.assertNotIn(">", location.official_name)

    def test_wiki_save_sanitizes_name(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = baker.make(Location, latitude=41.2, longitude=-75.2)
        wiki = baker.make(Wiki, location=location, name="<img src=x onerror=alert(1)>")
        wiki.refresh_from_db()
        self.assertNotIn("<", wiki.name)
        self.assertNotIn(">", wiki.name)

    def test_pin_alias_save_sanitizes_name(self) -> None:
        from urbanlens.dashboard.models.aliases.model import PinAlias
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(Location, latitude=41.3, longitude=-75.3)
        pin = baker.make(Pin, profile=self.profile, location=location, name="Real Name")
        alias = PinAlias.objects.create(pin=pin, name="<script>bad</script>")
        alias.refresh_from_db()
        self.assertNotIn("<", alias.name)
        self.assertNotIn(">", alias.name)
