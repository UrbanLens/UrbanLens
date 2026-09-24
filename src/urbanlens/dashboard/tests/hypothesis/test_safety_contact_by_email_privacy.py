"""An emergency contact added by email must not tell the check-in owner whether the address has an account."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.safety.model import EmergencyContactDefault, SafetyCheckin
from urbanlens.dashboard.services.visits.safety import (
    escalate_checkin,
    mark_found_safe,
    save_contact_defaults,
    set_checkin_contacts,
)


def _verified(username: str, email: str) -> User:
    user = baker.make(User, username=username, email=email, is_active=True)
    user.profile.verified_primary_email = user.profile.primary_email_normalized
    user.profile.save(update_fields=["verified_primary_email"])
    return user


class EmergencyContactByEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make(User, username="hiker").profile
        self.member = _verified("secret_member", "member@example.com")
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.owner,
            title="Tunnel walk",
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            destination_latitude="40.000000",
            destination_longitude="-74.000000",
        )

    def test_a_contact_added_by_email_stays_an_email_contact(self) -> None:
        set_checkin_contacts(self.checkin, [(None, "member@example.com", "")])

        contact = self.checkin.contacts.get()
        self.assertIsNone(contact.contact_profile_id)
        self.assertEqual(contact.email, "member@example.com")
        self.assertEqual(contact.display_name, "member@example.com")

    def test_a_default_contact_added_by_email_stays_an_email_contact(self) -> None:
        save_contact_defaults(self.owner, [(None, "member@example.com", "")])

        default = EmergencyContactDefault.objects.get(owner=self.owner)
        self.assertIsNone(default.contact_profile_id)

    def test_the_account_that_verified_the_address_is_still_alerted_in_app(self) -> None:
        set_checkin_contacts(self.checkin, [(None, "member@example.com", "")])

        escalate_checkin(self.checkin)

        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.member.profile, notification_type=NotificationType.SAFETY_CHECKIN_OVERDUE
            ).exists()
        )
        self.assertIn("member@example.com", [recipient for message in mail.outbox for recipient in message.to])

    def test_an_unverified_primary_address_gets_the_email_but_no_in_app_alert(self) -> None:
        squatter = baker.make(User, username="squatter", email="unproven@example.com", is_active=True)
        set_checkin_contacts(self.checkin, [(None, "unproven@example.com", "")])

        escalate_checkin(self.checkin)

        self.assertFalse(NotificationLog.objects.filter(profile=squatter.profile).exists())
        self.assertIn("unproven@example.com", [recipient for message in mail.outbox for recipient in message.to])

    def test_the_account_that_verified_the_address_is_told_when_the_owner_is_found(self) -> None:
        set_checkin_contacts(self.checkin, [(None, "member@example.com", ""), (None, "reporter@example.com", "")])
        reporter = self.checkin.contacts.get(email="reporter@example.com")

        mark_found_safe(reporter)

        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.member.profile, notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED
            ).exists()
        )
        self.assertIn("member@example.com", [recipient for message in mail.outbox for recipient in message.to])
