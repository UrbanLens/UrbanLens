"""Inbox dismissal for actionable notifications, and the history page."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.services.notifications.notification_center import dismiss_notification, inbox_notifications
from urbanlens.dashboard.services.visits.visits import accept_visit_suggestion, reject_visit_suggestion


class NotificationInboxFilterTests(TestCase):
    """Dismissed rows leave the bell inbox but remain in for_profile queries."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.active = baker.make(
            NotificationLog,
            profile=self.profile,
            status=Status.UNREAD,
            title="Active",
        )
        self.dismissed = baker.make(
            NotificationLog,
            profile=self.profile,
            status=Status.DISMISSED,
            title="Done",
        )

    def test_for_inbox_excludes_dismissed(self) -> None:
        qs = NotificationLog.objects.for_profile(self.profile).for_inbox()
        self.assertIn(self.active, qs)
        self.assertNotIn(self.dismissed, qs)

    def test_inbox_notifications_helper_excludes_dismissed(self) -> None:
        rows = inbox_notifications(self.profile)
        self.assertEqual([n.pk for n in rows], [self.active.pk])

    def test_dismiss_notification_marks_dismissed(self) -> None:
        self.assertTrue(dismiss_notification(self.active.pk))
        self.active.refresh_from_db()
        self.assertEqual(self.active.status, Status.DISMISSED)

    def test_dismiss_notification_none_is_noop(self) -> None:
        self.assertFalse(dismiss_notification(None))


class VisitSuggestionDismissesNotificationTests(TestCase):
    """Accepting or rejecting a visit suggestion retires its notification."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.notification = baker.make(
            NotificationLog,
            profile=self.profile,
            status=Status.UNREAD,
            notification_type=NotificationType.VISIT_SUGGESTED,
            title="Confirm your visit?",
        )
        origin_image = baker.make("dashboard.Image", profile=self.profile)
        self.suggestion = baker.make(
            VisitSuggestion,
            suggested_to=self.profile,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            visited_at=timezone.now(),
            status=VisitSuggestionStatus.PENDING,
            notification=self.notification,
            origin_image=origin_image,
        )

    def test_accept_dismisses_linked_notification(self) -> None:
        visit = accept_visit_suggestion(self.suggestion, self.profile)
        self.assertIsNotNone(visit)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.DISMISSED)

    def test_reject_dismisses_linked_notification(self) -> None:
        reject_visit_suggestion(self.suggestion)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.DISMISSED)

    def test_respond_view_removes_row_from_inbox_surface(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("visit_suggestion.respond", args=[self.suggestion.pk]),
            {"action": "reject", "surface": "inbox"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.DISMISSED)

    def test_respond_view_rerenders_history_row(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("visit_suggestion.respond", args=[self.suggestion.pk]),
            {"action": "reject", "surface": "history"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You dismissed this suggestion")
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.DISMISSED)


class NotificationDropdownAndHistoryTests(TestCase):
    """Dropdown hides dismissed rows; the history page still lists them."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.active = baker.make(
            NotificationLog,
            profile=self.profile,
            status=Status.UNREAD,
            title="Still open",
        )
        self.dismissed = baker.make(
            NotificationLog,
            profile=self.profile,
            status=Status.DISMISSED,
            title="Already handled",
            notification_type=NotificationType.VISIT_SUGGESTED,
        )

    def test_dropdown_excludes_dismissed(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications.dropdown"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Still open")
        self.assertNotContains(response, "Already handled")
        self.assertContains(response, "View all")

    def test_history_page_includes_dismissed(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Still open")
        self.assertContains(response, "Already handled")


class FriendRequestDismissFromProfileTests(TestCase):
    """Acting on a friend request outside the bell also dismisses it."""

    def setUp(self) -> None:
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

    def test_accept_via_service_dismisses_notification(self) -> None:
        from urbanlens.dashboard.services.social.friendship import accept_friend_request

        accept_friend_request(self.recipient, self.sender)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.DISMISSED)
