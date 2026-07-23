"""Tests for live notification push (dashboard/models/notifications/signals.py).

Covers:
- notification_group_name() - group naming and channel-layer validity
- as_push_payload()         - payload shape and message truncation
- push_notification_to_browser() - broadcast on create, not on update; failure tolerance
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

from hypothesis import given
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.notifications import signals as push_signals
from urbanlens.dashboard.models.notifications.model import NotificationLog


class NotificationGroupNameTests(SimpleTestCase):
    """notification_group_name() produces stable, channel-layer-safe names."""

    @given(st.integers(min_value=1, max_value=10**12))
    def test_group_name_format(self, profile_id: int):
        self.assertEqual(push_signals.notification_group_name(profile_id), f"profile_notifications_{profile_id}")

    @given(st.integers(min_value=1, max_value=10**12))
    def test_group_name_is_valid_channel_layer_group(self, profile_id: int):
        # channels requires group names to match this pattern and be < 100 chars
        name = push_signals.notification_group_name(profile_id)
        self.assertIsNotNone(re.fullmatch(r"[a-zA-Z0-9_\-.]{1,99}", name))

    @given(st.integers(min_value=1, max_value=10**6), st.integers(min_value=1, max_value=10**6))
    def test_distinct_profiles_get_distinct_groups(self, a: int, b: int):
        if a != b:
            self.assertNotEqual(push_signals.notification_group_name(a), push_signals.notification_group_name(b))


class AsPushPayloadTests(SimpleTestCase):
    """as_push_payload() forwards the toast fields and truncates long messages."""

    @given(st.text(max_size=2000))
    def test_message_never_exceeds_push_limit(self, message: str):
        notification = NotificationLog(id=1, title="Title", message=message, url="")
        payload = push_signals.as_push_payload(notification)
        # +1 allows for the appended ellipsis character
        self.assertLessEqual(len(payload["message"]), push_signals.PUSH_MESSAGE_LIMIT + 1)

    @given(st.text(max_size=push_signals.PUSH_MESSAGE_LIMIT))
    def test_short_message_passes_through_unchanged(self, message: str):
        notification = NotificationLog(id=1, title="Title", message=message, url="")
        payload = push_signals.as_push_payload(notification)
        self.assertEqual(payload["message"], message)

    def test_payload_contains_toast_fields(self):
        notification = NotificationLog(id=7, title="Hello", message="Body", url="/dashboard/map/")
        payload = push_signals.as_push_payload(notification)
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["title"], "Hello")
        self.assertEqual(payload["message"], "Body")
        self.assertEqual(payload["url"], "/dashboard/map/")
        self.assertIn("notification_type", payload)
        self.assertIn("importance", payload)


class PushSignalTests(TestCase):
    """The post_save receiver broadcasts inserts (and only inserts) after commit."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.layer = MagicMock()
        self.layer.group_send = AsyncMock()

    def test_creating_notification_broadcasts_to_profile_group(self):
        with patch.object(push_signals, "get_channel_layer", return_value=self.layer):
            with self.captureOnCommitCallbacks(execute=True):
                notification = baker.make(NotificationLog, profile=self.profile, title="Hi", message="Body", url="/x/")

        self.layer.group_send.assert_awaited_once()
        group, event = self.layer.group_send.await_args.args
        self.assertEqual(group, f"profile_notifications_{self.profile.pk}")
        self.assertEqual(event["type"], "notification.new")
        self.assertEqual(event["notification"]["id"], notification.pk)
        self.assertEqual(event["notification"]["title"], "Hi")
        self.assertEqual(event["notification"]["url"], "/x/")

    def test_updating_notification_does_not_broadcast(self):
        with patch.object(push_signals, "get_channel_layer", return_value=self.layer):
            with self.captureOnCommitCallbacks(execute=True):
                notification = baker.make(NotificationLog, profile=self.profile)
            self.layer.group_send.reset_mock()

            with self.captureOnCommitCallbacks(execute=True):
                notification.title = "changed"
                notification.save()

        self.layer.group_send.assert_not_awaited()

    def test_notification_without_profile_does_not_broadcast(self):
        with patch.object(push_signals, "get_channel_layer", return_value=self.layer):
            with self.captureOnCommitCallbacks(execute=True):
                baker.make(NotificationLog, profile=None)

        self.layer.group_send.assert_not_awaited()

    def test_channel_layer_failure_does_not_break_creation(self):
        self.layer.group_send = AsyncMock(side_effect=RuntimeError("valkey down"))
        with patch.object(push_signals, "get_channel_layer", return_value=self.layer):
            with self.captureOnCommitCallbacks(execute=True):
                notification = baker.make(NotificationLog, profile=self.profile)

        self.assertTrue(NotificationLog.objects.filter(pk=notification.pk).exists())

    def test_missing_channel_layer_does_not_break_creation(self):
        with patch.object(push_signals, "get_channel_layer", return_value=None):
            with self.captureOnCommitCallbacks(execute=True):
                notification = baker.make(NotificationLog, profile=self.profile)

        self.assertTrue(NotificationLog.objects.filter(pk=notification.pk).exists())
