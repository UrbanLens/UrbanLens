"""An end-of-check-in notice that fails to build or send is sent by a later sweep, once, rather than lost.

Each contact is claimed (``resolution_notified_at``) before it is told, so two senders can never both tell it. A
claim that outlives a failure would mean someone searching is never told to stop, so a failure must leave the
contact for the next sweep - without a second email, or a second in-app notice, for anyone already told.
"""

from __future__ import annotations

import datetime
import smtplib
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string as real_render_to_string
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import notification_emails_sent
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyCheckinStatus
from urbanlens.dashboard.services.undo.service import undo_latest
from urbanlens.dashboard.services.visits import safety as safety_service
from urbanlens.dashboard.services.visits.safety import check_in, delete_checkin
from urbanlens.dashboard.tasks import escalate_overdue_checkins

_ALL_CLEAR = "dashboard/email/safety_checkin_all_clear.html"


class _RetryTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, username="hiker", email="hiker@example.com").profile
        self.friend = baker.make(User, username="friend", email="friend@example.com").profile
        now = timezone.now()
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.owner,
            title="Tunnel walk",
            checkin_by=now - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            status=SafetyCheckinStatus.OVERDUE,
            escalated_at=now,
            notify_community_wiki=False,
        )
        self.by_account = baker.make(
            SafetyCheckinContact, checkin=self.checkin, contact_profile=self.friend, email=None, notified_at=now
        )
        self.by_email = baker.make(
            SafetyCheckinContact,
            checkin=self.checkin,
            email="rescuer@example.com",
            contact_profile=None,
            notified_at=now,
        )

    def _resolved(self, ago: datetime.timedelta) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(resolved_at=timezone.now() - ago)

    def _sweep(self) -> None:
        with notification_emails_sent():
            escalate_overdue_checkins()

    def _emails_to(self, address: str) -> int:
        return sum(address in message.to for message in mail.outbox)

    def _in_app(self, profile: Profile) -> int:
        return NotificationLog.objects.filter(
            profile=profile, notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED
        ).count()


class FailedBuildTests(_RetryTestCase):
    def test_a_template_failure_leaves_the_contact_for_the_next_sweep_which_sends_it_once(self) -> None:
        def broken_all_clear(template, *args, **kwargs):
            if template == _ALL_CLEAR:
                raise ValueError("template bug")
            return real_render_to_string(template, *args, **kwargs)

        with notification_emails_sent(), mock.patch.object(safety_service, "render_to_string", broken_all_clear):
            check_in(self.checkin, self.owner)

        self.assertEqual(self._emails_to("rescuer@example.com"), 0)
        self.assertEqual(self._in_app(self.friend), 0)
        self.assertEqual(
            set(
                SafetyCheckinContact.objects.filter(checkin=self.checkin).values_list(
                    "resolution_notified_at", flat=True
                )
            ),
            {None},
        )

        self._resolved(ago=datetime.timedelta(minutes=10))
        self._sweep()
        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._emails_to("friend@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)


class FailedSendTests(_RetryTestCase):
    def test_a_refused_send_is_sent_again_by_the_next_sweep_with_no_second_in_app_notice(self) -> None:
        real_send = EmailMultiAlternatives.send
        refused: list[str] = []

        def refuse_once(message, *args, **kwargs):
            if "rescuer@example.com" in message.to and not refused:
                refused.append(message.subject)
                raise smtplib.SMTPServerDisconnected("connection dropped")
            return real_send(message, *args, **kwargs)

        # Outermost, so it is still in place when the queued email is sent on leaving the inner block.
        with mock.patch.object(EmailMultiAlternatives, "send", refuse_once), notification_emails_sent():
            check_in(self.checkin, self.owner)

        self.assertEqual(len(refused), 1)
        self.assertEqual(self._emails_to("rescuer@example.com"), 0)

        self._resolved(ago=datetime.timedelta(minutes=10))
        self._sweep()
        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._emails_to("friend@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)


class SweepBoundsTests(_RetryTestCase):
    """The sweep only finishes what a resolution left undone, and only while the notice still means something."""

    def _resolve_without_notices(self, status: str = SafetyCheckinStatus.CHECKED_IN) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=status, resolved_at=timezone.now())

    def test_it_leaves_a_resolution_still_in_progress_to_its_own_request(self) -> None:
        """Deleting a check-in resolves it first; a sweep that raced that request would send the wrong wording."""
        self._resolve_without_notices()

        self._sweep()

        self.assertEqual(len(mail.outbox), 0)

    def test_it_does_not_send_hours_after_the_fact(self) -> None:
        self._resolve_without_notices()
        self._resolved(ago=datetime.timedelta(hours=3))

        self._sweep()

        self.assertEqual(len(mail.outbox), 0)

    def test_it_never_tells_the_contact_who_reported_the_owner_found(self) -> None:
        self._resolve_without_notices(SafetyCheckinStatus.FOUND_SAFE)
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(resolved_by_label="rescuer")
        SafetyCheckinContact.objects.filter(pk=self.by_email.pk).update(found_safe_at=timezone.now())
        self._resolved(ago=datetime.timedelta(minutes=10))

        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 0)
        self.assertEqual(self._emails_to("friend@example.com"), 1)


class UndoneDeletionTests(_RetryTestCase):
    def test_a_deleted_then_restored_checkin_does_not_tell_its_contacts_again(self) -> None:
        """Deleting it told them it was removed; the restored contacts must still read as told."""
        with notification_emails_sent():
            delete_checkin(self.checkin, self.owner)
        undo_latest(self.owner)
        SafetyCheckin.objects.filter(profile=self.owner).update(
            resolved_at=timezone.now() - datetime.timedelta(minutes=10)
        )

        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)
