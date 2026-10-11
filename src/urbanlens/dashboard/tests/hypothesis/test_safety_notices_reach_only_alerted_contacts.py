"""A contact hears about a check-in only once they were alerted that its owner missed it."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import notification_emails_sent
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
    SafetyContactOptOutScope,
)
from urbanlens.dashboard.services.visits.safety import (
    escalate_checkin,
    mark_found_safe,
    mark_found_safe_by_partner,
    notify_contacts_of_update,
    record_contact_opt_out,
)


class _NoticeTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, username="hiker", email="hiker@example.com").profile
        self.friend = baker.make(User, username="friend", email="friend@example.com").profile
        self.partner = baker.make(User, username="partner", email="partner@example.com").profile
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.owner,
            title="Tunnel walk",
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            notify_community_wiki=False,
        )
        baker.make(
            SafetyCheckinPartner,
            checkin=self.checkin,
            profile=self.partner,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )
        self.by_account = baker.make(
            SafetyCheckinContact, checkin=self.checkin, contact_profile=self.friend, email=None
        )
        self.by_email = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email="rescuer@example.com", contact_profile=None
        )

    def _emailed(self) -> set[str]:
        return {recipient.lower() for message in mail.outbox for recipient in message.to}

    def _alerted_in_app(self, profile: Profile, notification_type: str) -> bool:
        return NotificationLog.objects.filter(profile=profile, notification_type=notification_type).exists()

    def _mark_alerted(self, *contacts: SafetyCheckinContact) -> None:
        now = timezone.now()
        SafetyCheckinContact.objects.filter(pk__in=[contact.pk for contact in contacts]).update(notified_at=now)
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(escalated_at=now, status=SafetyCheckinStatus.OVERDUE)
        self.checkin.refresh_from_db()


class FoundSafeNoticeTests(_NoticeTestCase):
    def test_contacts_never_alerted_hear_nothing_when_a_partner_finds_the_owner_first(self) -> None:
        with notification_emails_sent():
            mark_found_safe_by_partner(self.checkin, self.partner)

        self.assertNotIn("friend@example.com", self._emailed())
        self.assertNotIn("rescuer@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.friend, NotificationType.SAFETY_CHECKIN_RESOLVED))
        # The owner is still told.
        self.assertIn("hiker@example.com", self._emailed())

    def test_alerted_contacts_are_told_the_owner_was_found(self) -> None:
        escalate_checkin(self.checkin)
        mail.outbox.clear()

        with notification_emails_sent():
            mark_found_safe_by_partner(self.checkin, self.partner)

        self.assertIn("friend@example.com", self._emailed())
        self.assertIn("rescuer@example.com", self._emailed())
        self.assertTrue(self._alerted_in_app(self.friend, NotificationType.SAFETY_CHECKIN_RESOLVED))

    def test_only_the_contacts_an_interrupted_escalation_reached_are_told(self) -> None:
        self._mark_alerted(self.by_email)

        with notification_emails_sent():
            mark_found_safe_by_partner(self.checkin, self.partner)

        self.assertIn("rescuer@example.com", self._emailed())
        self.assertNotIn("friend@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.friend, NotificationType.SAFETY_CHECKIN_RESOLVED))

    def test_an_alerted_contact_who_since_opted_out_is_not_told(self) -> None:
        self._mark_alerted(self.by_account, self.by_email)
        record_contact_opt_out(self.by_email, SafetyContactOptOutScope.CHECKIN)
        record_contact_opt_out(self.by_account, SafetyContactOptOutScope.CHECKIN)

        with notification_emails_sent():
            mark_found_safe_by_partner(self.checkin, self.partner)

        self.assertNotIn("rescuer@example.com", self._emailed())
        self.assertNotIn("friend@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.friend, NotificationType.SAFETY_CHECKIN_RESOLVED))

    def test_the_contact_who_reported_it_is_not_told_their_own_news(self) -> None:
        self._mark_alerted(self.by_account, self.by_email)

        with notification_emails_sent():
            mark_found_safe(self.by_email)

        self.assertNotIn("rescuer@example.com", self._emailed())
        self.assertIn("friend@example.com", self._emailed())


class PlanUpdateNoticeTests(_NoticeTestCase):
    def test_only_alerted_contacts_hear_of_a_plan_change(self) -> None:
        self._mark_alerted(self.by_email)

        with notification_emails_sent():
            notify_contacts_of_update(self.checkin, "updated their trip plan")

        self.assertIn("rescuer@example.com", self._emailed())
        self.assertNotIn("friend@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.friend, NotificationType.SAFETY_CHECKIN_PLAN_UPDATED))
