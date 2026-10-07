"""The owner of a safety check-in gets the final warning before any emergency contact is alerted, even across a missed beat tick."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyCheckinStatus
from urbanlens.dashboard.services.visits.safety import escalate_checkin, send_final_warning
from urbanlens.dashboard.tasks import escalate_overdue_checkins, send_final_checkin_warnings

_OWNER_EMAIL = "hiker@example.com"
_RESCUER_EMAIL = "rescuer@example.com"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class FinalWarningPrecedesEscalationTests(TestCase):
    """Drives the two five-minute beat sweeps at frozen instants around the check-in's overdue point.

    The cache is pinned to locmem because each sweep takes its overlap lock there."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, email=_OWNER_EMAIL).profile
        self.overdue_at = timezone.now().replace(microsecond=0) + timedelta(hours=2)
        self.checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN)
        self.contact = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email=_RESCUER_EMAIL, contact_profile=None
        )

    def _checkin(self, status: str) -> SafetyCheckin:
        return baker.make(
            SafetyCheckin,
            profile=self.owner,
            title="Tunnel walk",
            checkin_by=self.overdue_at - timedelta(hours=1),
            grace_period=timedelta(hours=1),
            status=status,
            notify_community_wiki=False,
        )

    def _at(self, offset: timedelta):
        return mock.patch("django.utils.timezone.now", return_value=self.overdue_at + offset)

    def _tick(self, offset: timedelta, *sweeps) -> None:
        """One beat tick: each sweep runs once, in the given order, at ``overdue_at + offset``."""
        with self._at(offset):
            for sweep in sweeps:
                sweep()

    def _warnings(self) -> int:
        return NotificationLog.objects.filter(
            profile=self.owner, notification_type=NotificationType.SAFETY_CHECKIN_FINAL_WARNING
        ).count()

    def _contact_alerted(self) -> bool:
        self.contact.refresh_from_db()
        return self.contact.notified_at is not None or any(_RESCUER_EMAIL in message.to for message in mail.outbox)

    def _recipients_in_order(self) -> list[str]:
        return [recipient for message in mail.outbox for recipient in message.to]

    def test_a_missed_warning_tick_still_warns_the_owner_before_the_contacts(self) -> None:
        self._tick(-timedelta(minutes=6), escalate_overdue_checkins, send_final_checkin_warnings)
        self.assertEqual(self._warnings(), 0)
        # The tick at -1 minute, the only one inside the warning window, never ran.

        # Escalation's sweep reaching the row first is the order that used to alert the contacts unwarned.
        self._tick(timedelta(minutes=4), escalate_overdue_checkins, send_final_checkin_warnings)

        self.assertEqual(self._warnings(), 1)
        self.assertFalse(self._contact_alerted())
        self.checkin.refresh_from_db()
        self.assertNotEqual(self.checkin.status, SafetyCheckinStatus.OVERDUE)

        self._tick(timedelta(minutes=9), escalate_overdue_checkins, send_final_checkin_warnings)

        self.assertTrue(self._contact_alerted())
        self.assertEqual(self._warnings(), 1)
        recipients = self._recipients_in_order()
        self.assertLess(recipients.index(_OWNER_EMAIL), recipients.index(_RESCUER_EMAIL))
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.OVERDUE)

    def test_a_late_warning_sent_first_in_the_tick_also_holds_escalation(self) -> None:
        self._tick(timedelta(minutes=4), send_final_checkin_warnings, escalate_overdue_checkins)

        self.assertEqual(self._warnings(), 1)
        self.assertFalse(self._contact_alerted())

    def test_a_late_warning_leaves_the_owner_minutes_not_seconds(self) -> None:
        self._tick(timedelta(minutes=4), send_final_checkin_warnings)

        self._tick(timedelta(minutes=6), escalate_overdue_checkins)
        self.assertFalse(self._contact_alerted())

        self._tick(timedelta(minutes=9), escalate_overdue_checkins)
        self.assertTrue(self._contact_alerted())

    def test_escalation_is_not_held_for_a_warning_that_never_goes_out(self) -> None:
        """No final-warning sweep runs at all, so the hold has to end by itself."""
        self._tick(timedelta(minutes=4), escalate_overdue_checkins)
        self.assertFalse(self._contact_alerted())

        self._tick(timedelta(minutes=14), escalate_overdue_checkins)
        self.assertTrue(self._contact_alerted())
        self.assertEqual(self._warnings(), 0)

    def test_escalation_is_not_held_by_a_warning_that_keeps_failing(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.visits.safety.NotificationLog.objects.notify",
            side_effect=RuntimeError("notification store down"),
        ):
            for minutes in (4, 9, 14, 19):
                self._tick(timedelta(minutes=minutes), send_final_checkin_warnings, escalate_overdue_checkins)

        self.assertTrue(self._contact_alerted())

    def test_a_failed_warning_is_retried_on_the_next_tick(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.visits.safety.NotificationLog.objects.notify",
            side_effect=RuntimeError("notification store down"),
        ):
            self._tick(-timedelta(minutes=1), send_final_checkin_warnings, escalate_overdue_checkins)

        self._tick(timedelta(minutes=4), escalate_overdue_checkins, send_final_checkin_warnings)

        self.assertEqual(self._warnings(), 1)
        self.assertFalse(self._contact_alerted())

    def test_the_on_time_flow_still_escalates_on_the_next_tick(self) -> None:
        self._tick(-timedelta(minutes=3), escalate_overdue_checkins, send_final_checkin_warnings)
        self.assertEqual(self._warnings(), 1)
        self.assertFalse(self._contact_alerted())

        self._tick(timedelta(minutes=2), escalate_overdue_checkins, send_final_checkin_warnings)

        self.assertTrue(self._contact_alerted())
        self.assertEqual(self._warnings(), 1)

    def test_an_unreminded_checkin_is_still_warned_before_escalation(self) -> None:
        """A reminder that never went out leaves the row SCHEDULED; it must not also cost the owner the final warning."""
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=SafetyCheckinStatus.SCHEDULED)

        self._tick(timedelta(minutes=4), escalate_overdue_checkins, send_final_checkin_warnings)

        self.assertEqual(self._warnings(), 1)
        self.assertFalse(self._contact_alerted())

    def test_no_final_warning_once_escalation_has_begun(self) -> None:
        self._tick(timedelta(minutes=14), escalate_overdue_checkins)
        self.assertTrue(self._contact_alerted())

        self._tick(timedelta(minutes=15), send_final_checkin_warnings)

        self.assertEqual(self._warnings(), 0)

    def test_a_stale_warning_sweep_cannot_warn_after_escalation_began(self) -> None:
        with self._at(timedelta(minutes=14)):
            stale = SafetyCheckin.objects.get(pk=self.checkin.pk)
            escalate_checkin(SafetyCheckin.objects.get(pk=self.checkin.pk))
            send_final_warning(stale)

        self.assertTrue(self._contact_alerted())
        self.assertEqual(self._warnings(), 0)

    def test_a_stale_escalation_sweep_waits_for_a_warning_claimed_first(self) -> None:
        with self._at(timedelta(minutes=4)):
            stale = SafetyCheckin.objects.get(pk=self.checkin.pk)
            send_final_warning(SafetyCheckin.objects.get(pk=self.checkin.pk))
            escalate_checkin(stale)

        self.assertEqual(self._warnings(), 1)
        self.assertFalse(self._contact_alerted())

    def test_duplicate_sweeps_send_one_warning(self) -> None:
        with self._at(timedelta(minutes=4)):
            stale = SafetyCheckin.objects.get(pk=self.checkin.pk)
            send_final_checkin_warnings()
            send_final_checkin_warnings()
            send_final_warning(stale)

        self.assertEqual(self._warnings(), 1)
        self.assertEqual(self._recipients_in_order().count(_OWNER_EMAIL), 1)

    def test_duplicate_escalation_sweeps_alert_each_contact_once(self) -> None:
        self._tick(timedelta(minutes=4), send_final_checkin_warnings)

        self._tick(timedelta(minutes=9), escalate_overdue_checkins, escalate_overdue_checkins)

        self.assertEqual(self._recipients_in_order().count(_RESCUER_EMAIL), 1)
