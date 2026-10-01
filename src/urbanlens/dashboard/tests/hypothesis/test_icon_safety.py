"""`Pin.icon` is validated on write, not only on render."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.core.icons import MAX_ICON_LENGTH, clean_icon
from urbanlens.dashboard.services.pins.pin_edit import apply_pin_edits
from urbanlens.dashboard.templatetags.dashboard_tags import is_icon_url, is_material_icon


class CleanIconTests(SimpleTestCase):
    def test_material_icon_names_are_kept(self) -> None:
        for name in ("factory", "door_front", "3d_rotation", "filter_1", "9mp"):
            self.assertEqual(clean_icon(name), name)

    def test_uploaded_icon_urls_are_kept(self) -> None:
        for url in ("/media/pin_custom_icons/x.png", "https://cdn.example/icon.png", "http://cdn.example/icon.png"):
            self.assertEqual(clean_icon(url), url)

    def test_emoji_are_kept(self) -> None:
        for emoji in ("🏚", "🏚️", "👩‍🚒"):
            self.assertEqual(clean_icon(emoji), emoji)

    def test_every_icon_the_picker_offers_is_storable(self) -> None:
        """The contract that was missing: what the picker offers, the field accepts.

        ``_is_emoji_token`` is a heuristic about what an emoji looks like, and 29 of the catalogue's own 1,249
        entries do not look like one - the 14 keycaps, whose base code point is ASCII; ``!!`` and ``!?``, which
        are punctuation; and 13 letter-category glyphs (Greek, Cyrillic, Hebrew, CJK, kana)."""
        from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES

        offered = [icon for _label, pairs in ICON_CATEGORIES.values() for icon, _ in pairs]
        refused = [icon for icon in offered if clean_icon(icon) != icon]

        self.assertEqual(
            refused,
            [],
            f"{len(refused)} of {len(offered)} catalogue icons would be discarded on write: {[ascii(icon) for icon in refused[:10]]}",
        )

    def test_the_catalogue_allowance_is_membership_not_a_looser_rule(self) -> None:
        """The bare ASCII a keycap is built on must stay refused.

        ``#`` and ``*`` are the base code points of two catalogue entries, and ``!!`` is what ``‼️`` reduces to."""
        for value in ("#", "*", "!!", "!?", "The Greek letter pi", "\u03b1\u03b2\u03b3 and some prose"):
            self.assertIsNone(clean_icon(value))

    def test_a_catalogue_icon_still_obeys_its_column_width(self) -> None:
        """``Label.icon`` is 50 wide and ``SavedFilter.icon`` 64; an allowance that
        skipped the length check would turn an accepted value into a DataError."""
        self.assertIsNone(clean_icon("0\ufe0f\u20e3", max_length=1))

    def test_blank_and_missing_fall_back_to_the_default(self) -> None:
        self.assertIsNone(clean_icon(None))
        self.assertIsNone(clean_icon(""))
        self.assertIsNone(clean_icon("   "))
        self.assertEqual(clean_icon("", default="place"), "place")

    def test_attribute_breaking_values_are_refused(self) -> None:
        """The `<img src>` branch is why this exists."""
        for hostile in (
            '/media/x.png" onerror="alert(1)',
            "/media/x.png' onload='alert(1)",
            "javascript:alert(1)",
            "data:text/html;base64,PHN2Zz4=",
            "/media/x.png\nonerror=alert(1)",
            "<img src=x onerror=alert(1)>",
        ):
            self.assertIsNone(clean_icon(hostile), f"{hostile!r} was accepted")

    def test_prose_is_refused(self) -> None:
        self.assertIsNone(clean_icon("a whole sentence about a factory"))
        self.assertIsNone(clean_icon("Factory"))  # Material names are lowercase

    def test_over_long_values_are_refused(self) -> None:
        self.assertIsNone(clean_icon("a" * (MAX_ICON_LENGTH + 1)))

    @given(st.text())
    def test_output_is_always_storable_and_classifiable(self, value: str) -> None:
        """Whatever survives must fit the column and be one of the three shapes.

        The renderers branch on shape - Material glyph, `<img>`, or plain text - so a stored value that matches
        neither of the first two is rendered as text."""
        cleaned = clean_icon(value)
        if cleaned is None:
            return
        self.assertLessEqual(len(cleaned), MAX_ICON_LENGTH)
        if is_icon_url(cleaned):
            self.assertNotIn('"', cleaned)
            self.assertNotIn("'", cleaned)
            self.assertNotIn("<", cleaned)
            self.assertFalse(any(char.isspace() for char in cleaned))
        else:
            self.assertTrue(is_material_icon(cleaned) or not cleaned.isascii())


class PinIconWritePathTests(TestCase):
    """Every pin write path stores a validated icon."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=41.0, longitude=-73.5)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)

    HOSTILE = '/media/x.png" onerror="alert(1)'

    def test_apply_pin_edits_refuses_a_hostile_icon(self) -> None:
        apply_pin_edits(self.pin, {"icon": self.HOSTILE})
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.icon)

    def test_apply_pin_edits_keeps_a_real_icon(self) -> None:
        apply_pin_edits(self.pin, {"icon": "factory"})
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.icon, "factory")

    def test_quick_edit_refuses_a_hostile_icon(self) -> None:
        response = self.client.post(
            reverse("pin.quick_edit", args=[self.pin.slug or self.pin.uuid]),
            data={"name": "Mill", "icon": self.HOSTILE},
        )
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.icon)

    def test_bulk_edit_refuses_a_hostile_icon(self) -> None:
        import json

        response = self.client.post(
            reverse("pin.bulk_edit"),
            data=json.dumps({"uuids": [str(self.pin.uuid)], "icon": self.HOSTILE}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.icon)

    def test_bulk_edit_keeps_a_real_icon(self) -> None:
        import json

        response = self.client.post(
            reverse("pin.bulk_edit"),
            data=json.dumps({"uuids": [str(self.pin.uuid)], "icon": "door_front"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.icon, "door_front")
