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
from urbanlens.dashboard.services.visits.safety import _resolve_as_found_safe, cancel_checkin, check_in, escalate_checkin, send_checkin_reminder


class CheckinResolvedMidSweepTests(TestCase):
    """A stale in-memory check-in must never overwrite a resolution."""

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
        contact = baker.make(SafetyCheckinContact, checkin=checkin, email="rescue@example.com", contact_profile=None, notified_at=None)
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
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN, checkin_by=self.now - timedelta(hours=2), notify_community_wiki=True)
        stale = SafetyCheckin.objects.get(pk=checkin.pk)

        check_in(SafetyCheckin.objects.get(pk=checkin.pk), self.profile)
        with mock.patch("urbanlens.dashboard.services.visits.safety.post_checkin_to_community_wiki") as post_to_wiki:
            escalate_checkin(stale)

        post_to_wiki.assert_not_called()

    def test_escalation_still_notifies_contacts_of_a_genuinely_overdue_checkin(self) -> None:
        checkin = self._checkin(SafetyCheckinStatus.AWAITING_CHECKIN, checkin_by=self.now - timedelta(hours=2))
        contact = baker.make(SafetyCheckinContact, checkin=checkin, email="rescue@example.com", contact_profile=None, notified_at=None)

        with mock.patch("urbanlens.dashboard.services.visits.safety._send_email") as send_email:
            escalate_checkin(checkin)

        contact.refresh_from_db()
        checkin.refresh_from_db()
        self.assertIsNotNone(contact.notified_at)
        send_email.assert_called_once()
        self.assertEqual(checkin.status, SafetyCheckinStatus.OVERDUE)


class OwnerResolutionIsAlsoCompareAndSetTests(TestCase):
    """The owner's own two transitions claim the row instead of overwriting it.

    ``check_in`` and ``cancel_checkin`` used to write ``status`` straight from
    a possibly-stale instance, unlike the three transitions above. Both only
    ever move a check-in *into* a terminal state, so the visible damage was
    limited to a wrong ``resolved_by_label`` - but having three of five
    lifecycle writes use compare-and-set and two not is the inconsistency that
    invites the next transition to copy the wrong one.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.profile,
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            title="night shoot",
            checkin_by=timezone.now() - timedelta(minutes=5),
            grace_period=timedelta(hours=1),
            notify_community_wiki=False,
        )

    def test_checking_in_does_not_overwrite_a_contact_who_already_reported_you_safe(self) -> None:
        stale = SafetyCheckin.objects.get(pk=self.checkin.pk)
        _resolve_as_found_safe(SafetyCheckin.objects.get(pk=self.checkin.pk), resolved_by_label="Dana")

        self.assertFalse(check_in(stale, self.profile))

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertEqual(self.checkin.resolved_by_label, "Dana")

    def test_cancelling_does_not_overwrite_an_existing_resolution(self) -> None:
        stale = SafetyCheckin.objects.get(pk=self.checkin.pk)
        _resolve_as_found_safe(SafetyCheckin.objects.get(pk=self.checkin.pk), resolved_by_label="Dana")

        self.assertFalse(cancel_checkin(stale))

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.FOUND_SAFE)

    def test_a_losing_check_in_leaves_the_instance_describing_the_stored_row(self) -> None:
        """The loser's in-memory copy must not claim a resolution it did not make."""
        stale = SafetyCheckin.objects.get(pk=self.checkin.pk)
        _resolve_as_found_safe(SafetyCheckin.objects.get(pk=self.checkin.pk), resolved_by_label="Dana")

        check_in(stale, self.profile)

        self.assertNotEqual(stale.status, SafetyCheckinStatus.CHECKED_IN)

    def test_an_uncontested_check_in_still_resolves_normally(self) -> None:
        self.assertTrue(check_in(self.checkin, self.profile))
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.CHECKED_IN)
        self.assertEqual(self.checkin.resolved_by_label, "you")
        self.assertIsNotNone(self.checkin.resolved_at)

    def test_an_uncontested_cancel_still_resolves_normally(self) -> None:
        self.assertTrue(cancel_checkin(self.checkin))
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.CANCELLED)
        self.assertEqual(self.checkin.resolved_by_label, "cancelled by owner")
