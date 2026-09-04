"""A check-in resolved mid-sweep must not be dragged back into the escalation path.

The three safety beat tasks fetch their rows, then spend real time per row -
rendering and sending email, one contact at a time. The owner checking in during
that window is not an exotic race: the reminder sweep runs precisely when the
check-in is due, which is exactly when owners act.

``_resolve_as_found_safe`` already guards its transition with a conditional
UPDATE. The sweep-driven transitions wrote ``status`` straight from their
in-memory instance, so a resolved row could be pushed back to
``AWAITING_CHECKIN`` - and ``SafetyCheckin.objects.overdue()`` selects on
exactly that status, so the next tick would call every emergency contact for
someone who had already checked in.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyCheckinStatus
from urbanlens.dashboard.services.visits.safety import cancel_checkin, check_in, escalate_checkin, send_checkin_reminder


class _CheckinRaceTestCase(TestCase):
    """Shared fixtures for the resolution-race tests below."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.now = timezone.now()

    def _checkin(self, status: str, **extra) -> SafetyCheckin:
        fields = {
            "title": "night shoot",
            "checkin_by": self.now - timedelta(minutes=5),
            "grace_period": timedelta(hours=1),
            "notify_community_wiki": False,
            **extra,
        }
        return baker.make(SafetyCheckin, profile=self.profile, status=status, **fields)


class CheckinResolvedMidSweepTests(_CheckinRaceTestCase):
    """A stale in-memory check-in must never overwrite a resolution."""

    def test_reminder_does_not_resurrect_a_checkin_resolved_mid_send(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.SCHEDULED)
        # The sweep's copy, read before the owner checked in.
        stale = SafetyCheckin.objects.get(pk=checkin.pk)

        check_in(SafetyCheckin.objects.get(pk=checkin.pk), self.profile)
        send_checkin_reminder(stale)

        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)
        self.assertNotIn(checkin, SafetyCheckin.objects.overdue())

    def test_reminder_still_advances_an_unresolved_checkin(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.SCHEDULED)

        send_checkin_reminder(checkin)

        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.AWAITING_CHECKIN)
        self.assertIsNotNone(checkin.reminder_sent_at)

    def test_escalation_does_not_notify_contacts_of_a_checkin_resolved_mid_sweep(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN, checkin_by=self.now - timedelta(hours=2))
        contact = baker.make(
            SafetyCheckinContact, checkin=checkin, email="rescue@example.com", contact_profile=None, notified_at=None
        )
        stale = SafetyCheckin.objects.get(pk=checkin.pk)

        check_in(SafetyCheckin.objects.get(pk=checkin.pk), self.profile)
        with mock.patch("urbanlens.dashboard.services.visits.safety._send_email") as send_email:
            escalate_checkin(stale)

        contact.refresh_from_db()
        checkin.refresh_from_db()
        self.assertIsNone(contact.notified_at)
        send_email.assert_not_called()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)

    def test_escalation_does_not_publish_a_resolved_checkin_to_the_community_wiki(self) -> None:
        checkin = self._checkin(
            SafetyCheckinStatus.AWAITING_CHECKIN, checkin_by=self.now - timedelta(hours=2), notify_community_wiki=True
        )
        stale = SafetyCheckin.objects.get(pk=checkin.pk)

        check_in(SafetyCheckin.objects.get(pk=checkin.pk), self.profile)
        with mock.patch("urbanlens.dashboard.services.visits.safety.post_checkin_to_community_wiki") as post_to_wiki:
            escalate_checkin(stale)

        post_to_wiki.assert_not_called()

    def test_escalation_still_notifies_contacts_of_a_genuinely_overdue_checkin(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN, checkin_by=self.now - timedelta(hours=2))
        contact = baker.make(
            SafetyCheckinContact, checkin=checkin, email="rescue@example.com", contact_profile=None, notified_at=None
        )

        with mock.patch("urbanlens.dashboard.services.visits.safety._send_email") as send_email:
            escalate_checkin(checkin)

        contact.refresh_from_db()
        checkin.refresh_from_db()
        self.assertIsNotNone(contact.notified_at)
        send_email.assert_called_once()
        self.assertEqual(checkin.status, SafetyCheckinStatus.OVERDUE)


class StaleInstanceResolutionTests(_CheckinRaceTestCase):
    """``check_in``/``cancel_checkin`` must not clobber a resolution that landed first.

    Both used to write ``status``/``resolved_at``/``resolved_by_label`` straight
    from their (possibly stale) in-memory instance. A contact marking the owner
    safe in the same moment would have their resolution overwritten and every
    side effect (broadcast, conclusion, archival scheduling) run twice.
    """

    def _resolve_behind_the_scenes(self, checkin: SafetyCheckin) -> None:
        """Resolve the DB row directly, bypassing the in-memory instance."""
        SafetyCheckin.objects.filter(pk=checkin.pk).update(
            status=SafetyCheckinStatus.FOUND_SAFE,
            resolved_at=timezone.now(),
            resolved_by_label="contact",
        )

    def test_check_in_loses_the_race_and_fires_no_side_effects(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN)
        stale = SafetyCheckin.objects.get(pk=checkin.pk)
        self._resolve_behind_the_scenes(checkin)

        with (
            mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_status_update") as broadcast,
            mock.patch("urbanlens.dashboard.services.visits.safety._conclude_checkin") as conclude,
            mock.patch("urbanlens.dashboard.services.visits.safety.schedule_checkin_archival") as archive,
        ):
            resolved = check_in(stale, self.profile)

        self.assertFalse(resolved)
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertEqual(checkin.resolved_by_label, "contact")
        broadcast.assert_not_called()
        conclude.assert_not_called()
        archive.assert_not_called()

    def test_cancel_checkin_loses_the_race_and_fires_no_side_effects(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN)
        stale = SafetyCheckin.objects.get(pk=checkin.pk)
        self._resolve_behind_the_scenes(checkin)

        with (
            mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_status_update") as broadcast,
            mock.patch("urbanlens.dashboard.services.visits.safety.schedule_checkin_archival") as archive,
        ):
            cancelled = cancel_checkin(stale)

        self.assertFalse(cancelled)
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertEqual(checkin.resolved_by_label, "contact")
        broadcast.assert_not_called()
        archive.assert_not_called()

    def test_check_in_wins_on_an_unresolved_checkin(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN)

        with (
            mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_status_update") as broadcast,
            mock.patch("urbanlens.dashboard.services.visits.safety.schedule_checkin_archival") as archive,
        ):
            resolved = check_in(checkin, self.profile)

        self.assertTrue(resolved)
        # In-memory instance is synced without a refresh, mirroring _resolve_as_found_safe.
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)
        self.assertEqual(checkin.resolved_by_label, "you")
        self.assertIsNotNone(checkin.resolved_at)
        broadcast.assert_called_once()
        archive.assert_called_once()

    def test_cancel_checkin_wins_on_an_unresolved_checkin(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.SCHEDULED)

        with (
            mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_status_update") as broadcast,
            mock.patch("urbanlens.dashboard.services.visits.safety.schedule_checkin_archival") as archive,
        ):
            cancelled = cancel_checkin(checkin)

        self.assertTrue(cancelled)
        self.assertEqual(checkin.status, SafetyCheckinStatus.CANCELLED)
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CANCELLED)
        self.assertEqual(checkin.resolved_by_label, "cancelled by owner")
        self.assertIsNotNone(checkin.resolved_at)
        broadcast.assert_called_once()
        archive.assert_called_once()
