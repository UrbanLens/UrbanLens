"""Tests for the editable privacy-hint icon (_privacy_hint.html).

Covers the icon lock/eye swap and the hidden `data-other-fields` snapshot
that lets a single-field change still pass PrivacySettingsForm's validation
(which requires all 8 of its fields, not just the one being edited here).
"""

from __future__ import annotations

import json
import re

from django.template.loader import render_to_string

from urbanlens.core.tests.testcase import TestCase


class _FakeProfile:
    """A minimal stand-in with just the fields _privacy_hint.html reads from `owner`."""

    def __init__(self, **overrides: str) -> None:
        self.profile_visibility = "friends"
        self.comment_visibility = "friends"
        self.friend_request_visibility = "anyone"
        self.photo_upload_visibility = "friends"
        self.trip_pin_location_visibility = "no_one"
        self.viewer_photo_filter = "anyone"
        self.contact_visibility = "no_one"
        self.direct_message_visibility = "friends"
        for key, value in overrides.items():
            setattr(self, key, value)


class PrivacyHintEditableTests(TestCase):
    def test_eye_icon_when_visible_to_anyone(self) -> None:
        owner = _FakeProfile(profile_visibility="anyone")
        html = render_to_string(
            "dashboard/partials/ui/_privacy_hint.html",
            {"label": "Name, avatar & bio", "value": "Anyone (Logged In)", "field": "profile_visibility", "raw_value": owner.profile_visibility, "owner": owner},
        )
        self.assertIn("visibility</i>", html)
        self.assertNotIn("lock</i>", html)

    def test_lock_icon_for_every_other_visibility_level(self) -> None:
        for value in ("anything_in_common", "common_pin", "common_friend", "common_trip", "friends", "no_one"):
            owner = _FakeProfile(profile_visibility=value)
            html = render_to_string(
                "dashboard/partials/ui/_privacy_hint.html",
                {"label": "Name, avatar & bio", "value": value, "field": "profile_visibility", "raw_value": owner.profile_visibility, "owner": owner},
            )
            self.assertIn("lock</i>", html, f"expected lock icon for {value}")
            self.assertNotIn("visibility</i>", html, f"unexpected eye icon for {value}")

    def test_static_non_editable_hint_defaults_to_lock_and_has_no_button(self) -> None:
        html = render_to_string("dashboard/partials/ui/_privacy_hint.html", {"text": "Only visible to you"})
        self.assertIn("lock</i>", html)
        self.assertNotIn("ul-privacy-hint-btn", html)
        self.assertNotIn("ul-privacy-hint-select", html)

    def test_other_fields_snapshot_includes_all_eight_privacy_form_fields(self) -> None:
        owner = _FakeProfile(profile_visibility="anyone", contact_visibility="friends")
        html = render_to_string(
            "dashboard/partials/ui/_privacy_hint.html",
            {"label": "Name, avatar & bio", "value": "Anyone (Logged In)", "field": "profile_visibility", "raw_value": owner.profile_visibility, "owner": owner},
        )
        match = re.search(r"data-other-fields='([^']*)'", html)
        self.assertIsNotNone(match)
        assert match is not None  # narrows type for the mypy pass below
        snapshot = json.loads(match.group(1))
        self.assertEqual(
            snapshot,
            {
                "profile_visibility": "anyone",
                "comment_visibility": "friends",
                "friend_request_visibility": "anyone",
                "photo_upload_visibility": "friends",
                "trip_pin_location_visibility": "no_one",
                "viewer_photo_filter": "anyone",
                "contact_visibility": "friends",
                "direct_message_visibility": "friends",
            },
        )

    def test_editable_select_marks_the_current_value_selected(self) -> None:
        owner = _FakeProfile(contact_visibility="common_friend")
        html = render_to_string(
            "dashboard/partials/ui/_privacy_hint.html",
            {"value": "Users with a friend in common", "field": "contact_visibility", "raw_value": owner.contact_visibility, "owner": owner},
        )
        self.assertIn('<option value="common_friend" selected>', html)
