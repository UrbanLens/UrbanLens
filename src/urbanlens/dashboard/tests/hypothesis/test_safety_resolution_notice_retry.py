"""An end-of-check-in notice that fails to build or send is sent by a later sweep, once, rather than lost.

Each contact is claimed (``resolution_notified_at``) before it is told, so two senders can never both tell it. A
claim that outlives a failure would mean someone searching is never told to stop, so a failure must leave the
contact for the next sweep - without a second email, or a second in-app notice, for anyone already told.
"""

from __future__ import annotations

import datetime
import shutil
import smtplib
import tempfile
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
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.import_export import import_data
from urbanlens.dashboard.services.undo.service import undo_latest
from urbanlens.dashboard.services.visits import safety as safety_service
from urbanlens.dashboard.services.visits.safety import (
    check_in,
    delete_checkin,
    mark_found_safe_by_partner,
    send_resolution_email,
)
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


def _refusing_once(address: str, refused: list[str]):
    """A stand-in for ``EmailMultiAlternatives.send`` whose first send to ``address`` fails as a dropped connection."""
    real_send = EmailMultiAlternatives.send

    def send(message, *args, **kwargs):
        if address in message.to and not refused:
            refused.append(message.subject)
            raise smtplib.SMTPServerDisconnected("connection dropped")
        return real_send(message, *args, **kwargs)

    return send


class FailedSendTests(_RetryTestCase):
    def test_a_refused_send_is_sent_again_by_the_next_sweep_with_no_second_in_app_notice(self) -> None:
        refused: list[str] = []

        # Outermost, so it is still in place when the queued email is sent on leaving the inner block.
        with (
            mock.patch.object(EmailMultiAlternatives, "send", _refusing_once("rescuer@example.com", refused)),
            notification_emails_sent(),
        ):
            check_in(self.checkin, self.owner)

        self.assertEqual(len(refused), 1)
        self.assertEqual(self._emails_to("rescuer@example.com"), 0)

        self._resolved(ago=datetime.timedelta(minutes=10))
        self._sweep()
        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._emails_to("friend@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)

    def test_an_account_contact_whose_email_was_refused_gets_the_email_again_and_one_in_app_notice(self) -> None:
        """Their in-app notice went out with the claim; the retry owes them the email alone."""
        refused: list[str] = []
        with (
            mock.patch.object(EmailMultiAlternatives, "send", _refusing_once("friend@example.com", refused)),
            notification_emails_sent(),
        ):
            check_in(self.checkin, self.owner)
        self.assertEqual((len(refused), self._emails_to("friend@example.com"), self._in_app(self.friend)), (1, 0, 1))

        self._resolved(ago=datetime.timedelta(minutes=10))
        self._sweep()
        self._sweep()

        self.assertEqual(self._emails_to("friend@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)


class DeletionTests(_RetryTestCase):
    """A deleted check-in leaves no row for a sweep to find, so its notice cannot wait for one."""

    def test_an_email_that_fails_to_build_still_goes_out_as_plain_text(self) -> None:
        def broken_all_clear(template, *args, **kwargs):
            if template == _ALL_CLEAR:
                raise ValueError("template bug")
            return real_render_to_string(template, *args, **kwargs)

        with notification_emails_sent(), mock.patch.object(safety_service, "render_to_string", broken_all_clear):
            delete_checkin(self.checkin, self.owner)

        (message,) = [message for message in mail.outbox if "rescuer@example.com" in message.to]
        self.assertIn("stop looking", message.subject)
        self.assertIn("removed their check-in", message.body)
        self.assertEqual(getattr(message, "alternatives", []), [])

    def test_a_refused_send_re_queues_itself_even_while_the_row_is_still_there(self) -> None:
        """The worker can fail before the delete commits; the row it would mark is about to go with it."""
        with (
            mock.patch.object(EmailMultiAlternatives, "send", side_effect=smtplib.SMTPServerDisconnected("down")),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            send_resolution_email(
                self.by_email.pk, to="rescuer@example.com", subject="s", text_body="t", html_body="", removed=True
            )

        enqueue.assert_called_once()
        self.by_email.refresh_from_db()
        self.assertIsNone(self.by_email.resolution_email_failed_at)

    def test_deleting_a_resolved_checkin_whose_notice_is_still_owed_sends_it_first(self) -> None:
        now = timezone.now()
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(
            status=SafetyCheckinStatus.CHECKED_IN, resolved_at=now, archive_scheduled_at=now
        )

        with notification_emails_sent():
            delete_checkin(SafetyCheckin.objects.get(pk=self.checkin.pk), self.owner)

        (message,) = [message for message in mail.outbox if "rescuer@example.com" in message.to]
        self.assertIn("removed their check-in", message.body)
        self.assertEqual(self._in_app(self.friend), 1)

    def test_a_refused_send_is_retried_by_the_email_task_itself(self) -> None:
        refused: list[str] = []

        with (
            mock.patch.object(EmailMultiAlternatives, "send", _refusing_once("rescuer@example.com", refused)),
            notification_emails_sent(),
        ):
            delete_checkin(self.checkin, self.owner)

        self.assertEqual(len(refused), 1)
        self.assertEqual(self._emails_to("rescuer@example.com"), 1)


class SweepBoundsTests(_RetryTestCase):
    """The sweep only finishes what a resolution left undone, and only while the notice still means something."""

    def _resolve_without_notices(self, status: str = SafetyCheckinStatus.CHECKED_IN) -> None:
        now = timezone.now()
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(
            status=status, resolved_at=now, archive_scheduled_at=now
        )

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

    def test_it_leaves_alone_a_checkin_this_site_never_resolved(self) -> None:
        """An imported check-in arrives resolved, with its contacts' alert times, but was never scheduled for archival."""
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(
            status=SafetyCheckinStatus.CANCELLED, resolved_at=timezone.now() - datetime.timedelta(minutes=10)
        )

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


class FailureBeforeTheNoticesTests(_RetryTestCase):
    """A resolution that fails part-way, before its own request told anyone, is still finished by the sweep."""

    def test_a_check_in_whose_visit_suggestion_fails_still_gets_its_contacts_told(self) -> None:
        with (
            notification_emails_sent(),
            mock.patch.object(safety_service, "_conclude_checkin", side_effect=RuntimeError("geocoder down")),
            self.assertRaises(RuntimeError),
        ):
            check_in(self.checkin, self.owner)
        self.assertEqual(self._emails_to("rescuer@example.com"), 0)

        self._resolved(ago=datetime.timedelta(minutes=10))
        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)

    def test_a_found_safe_whose_owner_notice_fails_still_gets_the_others_told(self) -> None:
        partner = baker.make(User, username="partner").profile
        baker.make(
            "dashboard.SafetyCheckinPartner",
            checkin=self.checkin,
            profile=partner,
            invited_by=self.owner,
            status="accepted",
        )
        real_notify = NotificationLog.objects.notify

        def notify(**kwargs):
            if kwargs.get("profile") == self.owner:
                raise RuntimeError("notification store down")
            return real_notify(**kwargs)

        with (
            notification_emails_sent(),
            mock.patch.object(NotificationLog.objects, "notify", side_effect=notify),
            self.assertRaises(RuntimeError),
        ):
            mark_found_safe_by_partner(self.checkin, partner)
        self.assertEqual(self._emails_to("rescuer@example.com"), 0)

        self._resolved(ago=datetime.timedelta(minutes=10))
        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)


class ImportedCheckinTests(TestCase):
    """The importer restores a check-in resolved, with its contacts' alert times; nothing may mail them for it."""

    def test_the_sweep_never_mails_the_contacts_of_an_imported_checkin(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        profile = baker.make(User, username="importer").profile
        data_dir = tempfile.mkdtemp(prefix="ul_safety_import_")
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        ctx = import_data.ImportContext(
            profile=profile, data_dir=data_dir, result=import_data.ImportResult(), pin_uuid_map={}, label_uuid_map={}
        )
        ten_minutes_ago = (timezone.now() - datetime.timedelta(minutes=10)).isoformat()
        row = {
            "title": "Imported walk",
            "checkin_by": (timezone.now() - datetime.timedelta(hours=2)).isoformat(),
            "status": SafetyCheckinStatus.CHECKED_IN,
            "escalated_at": ten_minutes_ago,
            "resolved_at": ten_minutes_ago,
            "contacts": [{"email": "imported@example.com", "notified_at": ten_minutes_ago}],
        }

        self.assertTrue(import_data.SafetyCheckinsImport().import_row(row, ctx))
        self.assertTrue(
            SafetyCheckinContact.objects.filter(email="imported@example.com", notified_at__isnull=False).exists()
        )
        with notification_emails_sent():
            escalate_overdue_checkins()

        self.assertEqual(len(mail.outbox), 0)


class UndoneDeletionTests(_RetryTestCase):
    def _restore_ten_minutes_after_resolution(self) -> None:
        undo_latest(self.owner)
        SafetyCheckin.objects.filter(profile=self.owner).update(
            resolved_at=timezone.now() - datetime.timedelta(minutes=10)
        )

    def test_a_deleted_then_restored_checkin_does_not_tell_its_contacts_again(self) -> None:
        """Deleting it told them it was removed; the restored contacts must still read as told."""
        with notification_emails_sent():
            delete_checkin(self.checkin, self.owner)
        self.assertEqual((self._emails_to("rescuer@example.com"), self._in_app(self.friend)), (1, 1))
        self._restore_ten_minutes_after_resolution()

        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
        self.assertEqual(self._in_app(self.friend), 1)

    def test_one_stashed_before_the_claim_was_saved_does_not_tell_them_again_either(self) -> None:
        with notification_emails_sent():
            delete_checkin(self.checkin, self.owner)
        action = UndoAction.objects.get(profile=self.owner)
        for entry in action.payload:
            for contact in entry["contacts"]:
                contact.pop("resolution_notified_at", None)
        action.save(update_fields=["payload"])
        self._restore_ten_minutes_after_resolution()

        self._sweep()

        self.assertEqual(self._emails_to("rescuer@example.com"), 1)
