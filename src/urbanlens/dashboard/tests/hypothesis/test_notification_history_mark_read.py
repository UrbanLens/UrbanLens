"""pin_shared and visit_suggested notifications must be markable read from
their own row, like every other notification type already is.

notification_item.html's friend_request, safety_ci_due, and generic (else)
branches all wire hx-post="{% url 'notifications.read' n.id %}" onto the <li>
itself, gated on n.is_unread. The pin_shared and visit_suggested branches did
not - the bell dropdown's own bulk-mark-read-on-GET masked this there, but
NotificationHistoryView deliberately does not bulk-mark-read (so unread rows
stay distinguishable in history), so a pending share/suggestion viewed only
via /notifications/ stayed UNREAD forever unless actually accepted/declined.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion


class PinSharedNotificationMarkReadTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.recipient_user = baker.make(User)
        self.recipient = self.recipient_user.profile
        self.sender = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.sender)
        self.notification = NotificationLog.objects.create(
            profile=self.recipient,
            status=Status.UNREAD,
            notification_type=NotificationType.PIN_SHARED,
            title="Pin shared with you",
            message=f"{self.sender.username} shared a pin with you.",
            source_profile=self.sender,
        )
        self.share = baker.make(
            PinShare,
            pin=self.pin,
            from_profile=self.sender,
            to_profile=self.recipient,
            status=PinShareStatus.PENDING,
            notification=self.notification,
        )
        self.client.force_login(self.recipient_user)

    def test_row_carries_the_mark_read_wiring(self) -> None:
        response = self.client.get(reverse("notifications.view"))

        body = response.content.decode()
        self.assertIn(f'hx-post="{reverse("notifications.read", args=[self.notification.pk])}"', body)

    def test_posting_to_the_read_endpoint_marks_it_read(self) -> None:
        response = self.client.post(reverse("notifications.read", args=[self.notification.pk]))

        self.assertEqual(response.status_code, 200)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.READ)


class VisitSuggestedNotificationMarkReadTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.recipient_user = baker.make(User)
        self.recipient = self.recipient_user.profile
        self.sender = baker.make(User).profile
        self.notification = NotificationLog.objects.create(
            profile=self.recipient,
            status=Status.UNREAD,
            notification_type=NotificationType.VISIT_SUGGESTED,
            title="Visit suggested",
            message=f"{self.sender.username} suggested a visit.",
            source_profile=self.sender,
        )
        self.suggestion = baker.make(
            VisitSuggestion,
            suggested_by=self.sender,
            suggested_to=self.recipient,
            notification=self.notification,
            # The model's db_visit_suggestion_exactly_one_origin constraint
            # requires exactly one of five origin fields set - from_my_activity
            # is the simplest to satisfy without another fixture row.
            origin_visit=None,
            trip_activity=None,
            safety_checkin=None,
            origin_image=None,
            from_my_activity=True,
        )
        self.client.force_login(self.recipient_user)

    def test_row_carries_the_mark_read_wiring(self) -> None:
        response = self.client.get(reverse("notifications.view"))

        body = response.content.decode()
        self.assertIn(f'hx-post="{reverse("notifications.read", args=[self.notification.pk])}"', body)

    def test_posting_to_the_read_endpoint_marks_it_read(self) -> None:
        response = self.client.post(reverse("notifications.read", args=[self.notification.pk]))

        self.assertEqual(response.status_code, 200)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, Status.READ)
