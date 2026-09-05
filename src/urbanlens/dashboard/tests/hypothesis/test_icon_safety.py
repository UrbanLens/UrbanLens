"""`Pin.icon` is validated on write, not only on render.

``Pin.icon`` was `CharField(max_length=255)` with no validator and no choices,
assigned straight from request data - the same shape colours had before
``services.core.colors.clean_color``. The map renders it into
``<img src="...">`` when it looks like a URL, so the client already tests
``^(https?://|/)`` and escapes the attribute; this covers the server half, so a
value that is none of the three shapes the field is meant to hold never reaches
the database at all.

See PROBLEMS.md, "`Pin.icon` is unvalidated free text rendered into a `src`
attribute".
"""

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

        The renderers branch on shape - Material glyph, `<img>`, or plain text -
        so a stored value that matches neither of the first two is rendered as
        text. Anything that reaches the `<img>` branch must therefore have
        passed the URL test, which is what this asserts for every input.
        """
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
