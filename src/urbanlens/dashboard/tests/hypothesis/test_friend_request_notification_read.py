"""Tests for UL-240: viewing a pending friend request should mark its notification read."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog


class FriendRequestNotificationReadOnViewTests(TestCase):
    """Merely viewing pending incoming requests should mark their notifications read."""

    def setUp(self) -> None:
        super().setUp()
        self.recipient_user = baker.make(User)
        self.recipient = self.recipient_user.profile
        self.sender = baker.make(User).profile
        Friendship.objects.create(
            from_profile=self.sender,
            to_profile=self.recipient,
            status=FriendshipStatus.REQUESTED,
        )
        self.notification = NotificationLog.objects.create(
            profile=self.recipient,
            status=Status.UNREAD,
            notification_type=NotificationType.FRIEND_REQUEST,
            title="New friend request",
            message="wants to be your friend.",
            source_profile=self.sender,
        )

    def test_friend_list_partial_marks_incoming_request_notification_read(self) -> None:
        self.client.force_login(self.recipient_user)

        response = self.client.get(reverse("friend.list", args=[self.recipient.pk]))

        self.assertEqual(response.status_code, 200)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.READ)

    def test_friends_page_marks_incoming_request_notification_read(self) -> None:
        self.client.force_login(self.recipient_user)

        response = self.client.get(reverse("friend.page", args=[self.recipient.pk]))

        self.assertEqual(response.status_code, 200)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.READ)

    def test_viewing_someone_elses_profile_does_not_mark_own_notification_read(self) -> None:
        """A non-owner viewer's HTMX request for `profile`'s widget must not touch the owner's notifications."""
        other_user = baker.make(User)
        self.client.force_login(other_user)

        response = self.client.get(reverse("friend.list", args=[self.recipient.pk]))

        self.assertEqual(response.status_code, 200)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.UNREAD)
