"""Tests for per-conversation direct message notification muting."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.direct_messages import conversations_for, create_direct_message


def _profile() -> Profile:
    user = baker.make("auth.User")
    return user.profile


def _set_dm_visibility(profile: Profile, visibility: str) -> None:
    profile.direct_message_visibility = visibility
    profile.save(update_fields=["direct_message_visibility"])


class DirectMessageMuteNotificationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.sender, VisibilityChoice.ANYONE)
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    def test_muted_sender_does_not_notify(self) -> None:
        DirectMessageMute.objects.create(viewer=self.recipient, sender=self.sender)
        create_direct_message(self.sender, self.recipient, "hello")
        self.assertFalse(NotificationLog.objects.filter(profile=self.recipient).exists())

    def test_unmuted_sender_still_notifies(self) -> None:
        create_direct_message(self.sender, self.recipient, "hello")
        self.assertTrue(NotificationLog.objects.filter(profile=self.recipient).exists())

    def test_mute_is_directional(self) -> None:
        """Muting the sender from the recipient's side doesn't mute the reverse conversation."""
        DirectMessageMute.objects.create(viewer=self.recipient, sender=self.sender)
        create_direct_message(self.recipient, self.sender, "reply")
        self.assertTrue(NotificationLog.objects.filter(profile=self.sender).exists())

    def test_message_is_still_delivered_while_muted(self) -> None:
        DirectMessageMute.objects.create(viewer=self.recipient, sender=self.sender)
        message = create_direct_message(self.sender, self.recipient, "hello")
        self.assertTrue(message.pk)
        self.assertTrue(message.is_unread)


class ConversationMuteToggleViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.viewer_user = baker.make("auth.User")
        self.viewer = self.viewer_user.profile
        self.partner = _profile()
        _set_dm_visibility(self.viewer, VisibilityChoice.ANYONE)
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)
        self.client.force_login(self.viewer_user)
        self.url = reverse("messages.mute", args=[self.partner.slug])

    def test_toggle_creates_mute(self) -> None:
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DirectMessageMute.objects.filter(viewer=self.viewer, sender=self.partner).exists())

    def test_toggle_again_removes_mute(self) -> None:
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertFalse(DirectMessageMute.objects.filter(viewer=self.viewer, sender=self.partner).exists())

    def test_muted_state_reflected_in_thread_response(self) -> None:
        response = self.client.post(self.url)
        self.assertContains(response, "Unmute notifications")

    def test_conversations_for_reports_muted_partner(self) -> None:
        create_direct_message(self.partner, self.viewer, "hi")
        DirectMessageMute.objects.create(viewer=self.viewer, sender=self.partner)
        conversations = conversations_for(self.viewer)
        self.assertTrue(conversations[0]["is_muted"])
