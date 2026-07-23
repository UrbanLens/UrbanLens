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


class FriendRequestResolutionStateTests(TestCase):
    """A friend_request notification whose underlying request has already been
    accepted or declined must reflect that instead of looking like a still-
    pending action item with no buttons and stale wording."""

    def setUp(self) -> None:
        super().setUp()
        self.recipient_user = baker.make(User)
        self.recipient = self.recipient_user.profile
        self.sender = baker.make(User).profile

    def _notification(self) -> NotificationLog:
        return NotificationLog.objects.create(
            profile=self.recipient,
            status=Status.READ,
            notification_type=NotificationType.FRIEND_REQUEST,
            title="New friend request",
            message="wants to be your friend.",
            source_profile=self.sender,
        )

    def test_pending_request_is_pending_and_unresolved(self) -> None:
        Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.REQUESTED)
        notification = self._notification()

        self.assertTrue(notification.is_friend_request_pending)
        self.assertIsNone(notification.friend_request_resolution)

    def test_accepted_request_is_no_longer_pending_and_resolution_is_accepted(self) -> None:
        Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.ACCEPTED)
        notification = self._notification()

        self.assertFalse(notification.is_friend_request_pending)
        self.assertEqual(notification.friend_request_resolution, "accepted")

    def test_declined_request_is_no_longer_pending_and_resolution_is_declined(self) -> None:
        Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.DECLINED)
        notification = self._notification()

        self.assertFalse(notification.is_friend_request_pending)
        self.assertEqual(notification.friend_request_resolution, "declined")

    def test_non_friend_request_notification_has_no_resolution(self) -> None:
        notification = NotificationLog.objects.create(
            profile=self.recipient,
            status=Status.READ,
            notification_type=NotificationType.FRIEND_ACCEPTED,
            title="Friend request accepted",
            message="accepted your friend request.",
            source_profile=self.sender,
        )

        self.assertFalse(notification.is_friend_request_pending)
        self.assertIsNone(notification.friend_request_resolution)

    def test_accept_response_view_leaves_notification_reflecting_accepted_state(self) -> None:
        """End-to-end: the notification dropdown's own respond endpoint (used
        when the user clicks Accept from the panel) must leave the original
        friend_request notification resolvable to "accepted" afterward, not
        just marked read with stale pending wording."""
        Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.REQUESTED)
        notification = self._notification()
        self.client.force_login(self.recipient_user)

        response = self.client.post(reverse("friend.respond", args=[self.sender.pk]), {"action": "accept"})

        self.assertEqual(response.status_code, 200)
        notification.refresh_from_db()
        self.assertFalse(notification.is_friend_request_pending)
        self.assertEqual(notification.friend_request_resolution, "accepted")
