"""Tests for the editable privacy-hint icon (_privacy_hint.html)."""

from __future__ import annotations

import re

from django.template.loader import render_to_string
from django.urls import reverse

from urbanlens.core.tests.testcase import SimpleTestCase

TEMPLATE = "dashboard/partials/ui/_privacy_hint.html"


def _editable(field: str, raw_value: str, **extra: str) -> str:
    return render_to_string(
        TEMPLATE, {"label": "Name, avatar & bio", "value": raw_value, "field": field, "raw_value": raw_value, **extra}
    )


class PrivacyHintEditableTests(SimpleTestCase):
    def test_eye_icon_when_visible_to_anyone(self) -> None:
        html = _editable("profile_visibility", "anyone")
        self.assertIn("visibility</i>", html)
        self.assertNotIn("lock</i>", html)

    def test_lock_icon_for_every_other_visibility_level(self) -> None:
        for value in ("anything_in_common", "common_pin", "common_friend", "common_trip", "friends", "no_one"):
            html = _editable("profile_visibility", value)
            self.assertIn("lock</i>", html, f"expected lock icon for {value}")
            self.assertNotIn("visibility</i>", html, f"unexpected eye icon for {value}")

    def test_static_non_editable_hint_defaults_to_lock_and_has_no_button(self) -> None:
        html = render_to_string(TEMPLATE, {"text": "Only visible to you"})
        self.assertIn("lock</i>", html)
        self.assertNotIn("ul-privacy-hint-btn", html)
        self.assertNotIn("ul-privacy-hint-select", html)

    def test_an_editable_hint_saves_only_its_own_field(self) -> None:
        """No snapshot of the other settings: a stale one reverted whichever another hint had just changed."""
        html = _editable("contact_visibility", "friends", note="Email only")
        self.assertIn(f'data-url="{reverse("settings.privacy_field", args=["contact_visibility"])}"', html)
        self.assertIn('data-hint-note="Email only"', html)
        self.assertNotIn("data-other-fields", html)
        self.assertIsNone(re.search(r"\bon[a-z]+=", html))
        self.assertNotIn("<script", html)

    def test_editable_select_marks_the_current_value_selected(self) -> None:
        html = _editable("contact_visibility", "common_friend")
        self.assertIn('<option value="common_friend" selected>', html)
