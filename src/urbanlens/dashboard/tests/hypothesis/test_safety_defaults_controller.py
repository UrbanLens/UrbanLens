"""Tests for the safety defaults form: chip-based contact parsing and autosave.

- _contact_display_label: pure function, tested without DB.
- SafetyHomeView.post: chip parsing (repeated contact_emails) and the XHR
  autosave JSON response, tested with RequestFactory + model_bakery.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import RequestFactory
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.safety import SafetyHomeView, _contact_display_label
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.safety.model import EmergencyContactDefault


class ContactDisplayLabelTests(TestCase):
    """_contact_display_label's fallback ordering: profile > label > email."""

    def test_linked_profile_wins_even_with_label_and_email(self) -> None:
        friend = SimpleNamespace(username="pixie")
        self.assertEqual(_contact_display_label(friend, "someone@example.com", "Someone"), "pixie")

    def test_label_wins_over_email_when_no_profile(self) -> None:
        self.assertEqual(_contact_display_label(None, "someone@example.com", "Someone"), "Someone")

    def test_falls_back_to_email_when_no_profile_or_label(self) -> None:
        self.assertEqual(_contact_display_label(None, "someone@example.com", ""), "someone@example.com")

    def test_empty_string_when_nothing_set(self) -> None:
        self.assertEqual(_contact_display_label(None, None, ""), "")


@settings(max_examples=25, deadline=None)
@given(
    label=st.text(min_size=0, max_size=30),
    email=st.emails(),
)
def test_contact_display_label_prefers_label_over_email_without_profile(label: str, email: str) -> None:
    """Without a linked profile, a non-empty label always wins over the email."""
    result = _contact_display_label(None, email, label)
    expected = label or email
    assert result == expected  # nosec B101


class SafetyHomeViewDefaultsPostTests(TestCase):
    """Chip-based contact parsing and the defaults autosave response."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.friend_profile = baker.make(User).profile
        Friendship.objects.create(
            from_profile=self.profile,
            to_profile=self.friend_profile,
            status=FriendshipStatus.ACCEPTED,
        )

    def _post(self, data: dict) -> object:
        req = self.factory.post("/safety/", data=data)
        req.user = self.user
        return SafetyHomeView.as_view()(req)

    def test_saves_a_friend_chip_and_multiple_email_chips(self) -> None:
        response = self._post(
            {
                "default_message": "Please check on me.",
                "grace_period_hours": "2",
                "contact_profile_ids": [str(self.friend_profile.pk)],
                "contact_emails": ["a@example.com", "b@example.com"],
            }
        )

        self.assertEqual(response.status_code, 302)
        saved = list(EmergencyContactDefault.objects.filter(owner=self.profile).order_by("order"))
        self.assertEqual(len(saved), 3)
        self.assertEqual(saved[0].contact_profile_id, self.friend_profile.pk)
        self.assertEqual({c.email for c in saved[1:]}, {"a@example.com", "b@example.com"})

    def test_ignores_a_friend_id_that_is_not_an_accepted_connection(self) -> None:
        stranger = baker.make(User).profile
        response = self._post(
            {
                "default_message": "",
                "grace_period_hours": "1",
                "contact_profile_ids": [str(stranger.pk)],
                "contact_emails": [],
            }
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmergencyContactDefault.objects.filter(owner=self.profile).count(), 0)

    def test_xhr_request_returns_json_summary_instead_of_redirecting(self) -> None:
        req = self.factory.post(
            "/safety/",
            data={
                "default_message": "Ping me if I go quiet.",
                "grace_period_hours": "1.5",
                "contact_profile_ids": [str(self.friend_profile.pk)],
                "contact_emails": [],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        req.user = self.user
        response = SafetyHomeView.as_view()(req)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["default_message"], "Ping me if I go quiet.")
        self.assertEqual(payload["default_grace_period_hours"], 1.5)
        self.assertEqual(payload["contact_labels"], [self.friend_profile.username])
