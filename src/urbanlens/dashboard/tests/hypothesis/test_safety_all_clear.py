"""Contacts alerted that a check-in's owner went missing are told when it is over, however it ends, exactly once."""

from __future__ import annotations

import datetime
from unittest import mock

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
from urbanlens.dashboard.services.visits import safety as safety_service
from urbanlens.dashboard.services.visits.safety import (
    _tell_alerted_contacts_it_is_over,
    cancel_checkin,
    check_in,
    delete_checkin,
    escalate_checkin,
    mark_found_safe_by_partner,
    record_contact_opt_out,
)

_PLAN = "Enter through the north gate, follow the fence line to the boiler house."


class _AllClearTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, username="hiker", email="hiker@example.com").profile
        self.friend = baker.make(User, username="friend", email="friend@example.com").profile
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.owner,
            title="Tunnel walk",
            plan_details=_PLAN,
            destination_latitude="42.123456",
            destination_longitude="-73.654321",
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            notify_community_wiki=False,
        )
        self.by_account = baker.make(
            SafetyCheckinContact, checkin=self.checkin, contact_profile=self.friend, email=None
        )
        self.by_email = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email="rescuer@example.com", contact_profile=None
        )
        self.unreached = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email="unreached@example.com", contact_profile=None
        )

    def _alert(self, *contacts: SafetyCheckinContact) -> None:
        """The state an escalation leaves behind once it has reached ``contacts``."""
        now = timezone.now()
        SafetyCheckinContact.objects.filter(pk__in=[contact.pk for contact in contacts]).update(notified_at=now)
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(escalated_at=now, status=SafetyCheckinStatus.OVERDUE)
        self.checkin.refresh_from_db()

    def _messages_to(self, address: str) -> list[mail.EmailMessage]:
        return [message for message in mail.outbox if address in message.to]

    def _html(self, message: mail.EmailMessage) -> str:
        return "".join(str(content) for content, _ in getattr(message, "alternatives", [])) + str(message.body)

    def _resolution_notices(self, profile: Profile) -> int:
        return NotificationLog.objects.filter(
            profile=profile, notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED
        ).count()


class AllClearTests(_AllClearTestCase):
    def test_a_late_check_in_tells_every_alerted_contact_to_stop_looking(self) -> None:
        self._alert(self.by_account, self.by_email)

        with notification_emails_sent():
            check_in(self.checkin, self.owner)

        for address in ("rescuer@example.com", "friend@example.com"):
            (message,) = self._messages_to(address)
            self.assertIn("stop looking", message.subject.lower())
            self.assertIn("checked in", self._html(message).lower())
        self.assertEqual(self._resolution_notices(self.friend), 1)

    def test_a_contact_escalation_never_reached_hears_nothing(self) -> None:
        self._alert(self.by_email)

        with notification_emails_sent():
            check_in(self.checkin, self.owner)

        self.assertEqual(self._messages_to("unreached@example.com"), [])
        self.assertEqual(self._messages_to("friend@example.com"), [])
        self.assertEqual(self._resolution_notices(self.friend), 0)

    def test_a_cancellation_after_escalation_is_an_all_clear_too(self) -> None:
        self._alert(self.by_account, self.by_email)

        with notification_emails_sent():
            cancel_checkin(self.checkin)

        (message,) = self._messages_to("rescuer@example.com")
        self.assertIn("stop looking", message.subject.lower())
        self.assertEqual(self._resolution_notices(self.friend), 1)

    def test_a_checkin_that_never_escalated_tells_no_contact_anything(self) -> None:
        with notification_emails_sent():
            check_in(self.checkin, self.owner)

        self.assertEqual([message for message in mail.outbox if set(message.to) - {"hiker@example.com"}], [])
        self.assertEqual(self._resolution_notices(self.friend), 0)

    def test_the_all_clear_carries_no_plan_or_location(self) -> None:
        self._alert(self.by_email)

        with notification_emails_sent():
            check_in(self.checkin, self.owner)

        html = self._html(self._messages_to("rescuer@example.com")[0])
        self.assertNotIn("north gate", html)
        self.assertNotIn("42.123456", html)
        self.assertNotIn("73.654321", html)

    def test_an_alerted_contact_who_opted_out_hears_nothing(self) -> None:
        self._alert(self.by_account, self.by_email)
        record_contact_opt_out(self.by_email, SafetyContactOptOutScope.CHECKIN)
        record_contact_opt_out(self.by_account, SafetyContactOptOutScope.CHECKIN)

        with notification_emails_sent():
            check_in(self.checkin, self.owner)

        self.assertEqual(self._messages_to("rescuer@example.com"), [])
        self.assertEqual(self._messages_to("friend@example.com"), [])
        self.assertEqual(self._resolution_notices(self.friend), 0)

    def test_a_found_safe_resolution_sends_its_own_notice_and_no_all_clear(self) -> None:
        partner = baker.make(User, username="partner", email="partner@example.com").profile
        baker.make(
            SafetyCheckinPartner,
            checkin=self.checkin,
            profile=partner,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )
        self._alert(self.by_account, self.by_email)

        with notification_emails_sent():
            mark_found_safe_by_partner(self.checkin, partner)
            check_in(SafetyCheckin.objects.get(pk=self.checkin.pk), self.owner)

        (message,) = self._messages_to("rescuer@example.com")
        self.assertIn("has been found", message.subject)
        self.assertEqual(self._resolution_notices(self.friend), 1)

    def test_a_second_resolution_attempt_sends_nothing_more(self) -> None:
        self._alert(self.by_email)

        with notification_emails_sent():
            check_in(self.checkin, self.owner)
            cancel_checkin(SafetyCheckin.objects.get(pk=self.checkin.pk))

        self.assertEqual(len(self._messages_to("rescuer@example.com")), 1)


class AllClearDuringEscalationTests(_AllClearTestCase):
    """The owner checks in while escalation is mailing a contact, after the check-in's own pass to alerted contacts ran."""

    def test_the_contact_alerted_in_that_moment_still_gets_the_all_clear(self) -> None:
        SafetyCheckinContact.objects.exclude(pk=self.by_email.pk).delete()
        real_send = safety_service._send_email
        checked_in: list[bool] = []

        def send_then_owner_checks_in(**kwargs) -> None:
            real_send(**kwargs)
            if kwargs["to"] == "rescuer@example.com" and not checked_in:
                checked_in.append(check_in(SafetyCheckin.objects.get(pk=self.checkin.pk), self.owner))

        with (
            notification_emails_sent(),
            mock.patch.object(safety_service, "_send_email", side_effect=send_then_owner_checks_in),
        ):
            escalate_checkin(self.checkin)

        self.assertEqual(checked_in, [True])
        subjects = [message.subject.lower() for message in self._messages_to("rescuer@example.com")]
        self.assertEqual(len(subjects), 2)
        self.assertIn("hasn't checked in", subjects[0])
        self.assertIn("stop looking", subjects[1])


class AllClearOnceTests(_AllClearTestCase):
    def test_a_second_pass_over_a_contact_already_told_sends_nothing(self) -> None:
        """The escalation's own catch and the resolver's pass can both see a contact alerted in the same instant."""
        self._alert(self.by_account, self.by_email)

        with notification_emails_sent():
            check_in(self.checkin, self.owner)
            _tell_alerted_contacts_it_is_over(
                SafetyCheckin.objects.get(pk=self.checkin.pk), SafetyCheckinContact.objects.filter(checkin=self.checkin)
            )

        self.assertEqual(len(self._messages_to("rescuer@example.com")), 1)
        self.assertEqual(self._resolution_notices(self.friend), 1)

    def test_one_contact_s_failure_neither_stops_the_others_nor_archival(self) -> None:
        self._alert(self.by_account, self.by_email)
        real_notify = NotificationLog.objects.notify

        def notify(**kwargs):
            if kwargs.get("profile") == self.friend:
                raise RuntimeError("notification store down")
            return real_notify(**kwargs)

        with notification_emails_sent(), mock.patch.object(NotificationLog.objects, "notify", side_effect=notify):
            check_in(self.checkin, self.owner)

        self.assertEqual(len(self._messages_to("rescuer@example.com")), 1)
        self.checkin.refresh_from_db()
        self.assertIsNotNone(self.checkin.archive_scheduled_at)


class AllClearOnDeletionTests(_AllClearTestCase):
    def test_deleting_an_escalated_checkin_says_it_ended_without_claiming_a_check_in(self) -> None:
        self._alert(self.by_email)

        with notification_emails_sent():
            delete_checkin(self.checkin, self.owner)

        (message,) = self._messages_to("rescuer@example.com")
        self.assertIn("stop looking", message.subject.lower())
        html = self._html(message).lower()
        self.assertNotIn("checked in", html)
        self.assertNotIn("is safe", html)
        self.assertNotIn("view check-in", html)
