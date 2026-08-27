"""Characterization and regression tests for the shared check-in edit service.

``services.visits.safety.apply_checkin_edit`` was extracted from
``controllers.safety.SafetyCheckinDetailView.post`` so the web autosave and the
external API's PATCH cannot drift apart. The first class here pins down the
*existing* locking semantics that extraction had to preserve exactly; the two
that follow cover defects the extraction fixed, and both fail against the
pre-extraction controller code:

* an edit on an already-archived check-in re-persisted plaintext PII onto a row
  whose PII had been deliberately scrubbed into an encrypted archive;
* lock flags were read off an already-loaded instance and written back without
  row-level locking, so an escalation landing mid-edit could be overwritten.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.services.visits.safety import (
    CheckinArchivedError,
    apply_checkin_edit,
    create_checkin,
    resolve_contact_inputs,
)


class _CheckinTestCase(TestCase):
    """Shared setup: an owner profile with one active, unlocked check-in."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.checkin = create_checkin(
            profile=self.profile,
            title="Original title",
            checkin_by=timezone.now() + datetime.timedelta(hours=4),
            grace_period=datetime.timedelta(hours=1),
            plan_details="Original plan",
            contact_message="Original message",
        )

    def _escalate(self) -> None:
        """Mark the check-in escalated directly in the DB, without touching the instance.

        Deliberately a queryset ``update``: it mimics what the escalation beat
        task does from another process, and leaves ``self.checkin`` holding the
        pre-escalation values - which is exactly the stale state the lock
        re-check has to defend against.
        """
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(escalated_at=timezone.now())


class CheckinEditLockCharacterizationTests(_CheckinTestCase):
    """The lock rules the extraction had to preserve verbatim."""

    def test_title_editable_while_unlocked(self) -> None:
        outcome = apply_checkin_edit(self.checkin, editor=self.profile, title="New title")
        self.assertEqual(self.checkin.title, "New title")
        self.assertEqual(outcome.warnings, [])

    def test_title_frozen_once_contacts_locked(self) -> None:
        """Escalation freezes the title - contacts were told to watch for that name."""
        self._escalate()
        outcome = apply_checkin_edit(self.checkin, editor=self.profile, title="Renamed after escalation")

        self.assertEqual(self.checkin.title, "Original title")
        self.assertTrue(any("Title is locked" in warning for warning in outcome.warnings))

    def test_message_and_wiki_flag_frozen_once_notifications_locked(self) -> None:
        self._escalate()
        outcome = apply_checkin_edit(
            self.checkin,
            editor=self.profile,
            contact_message="Rewritten message",
            notify_community_wiki=True,
        )

        self.assertEqual(self.checkin.contact_message, "Original message")
        self.assertFalse(self.checkin.notify_community_wiki)
        self.assertTrue(any("Message is locked" in warning for warning in outcome.warnings))
        self.assertTrue(any("Community wiki notification is locked" in warning for warning in outcome.warnings))

    def test_checking_in_locks_notifications_but_not_the_title(self) -> None:
        """``notifications_locked`` also trips on CHECKED_IN, while ``contacts_locked`` doesn't."""
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=SafetyCheckinStatus.CHECKED_IN)
        self.checkin.refresh_from_db()

        outcome = apply_checkin_edit(self.checkin, editor=self.profile, title="Post-checkin title", contact_message="Post-checkin message")

        self.assertEqual(self.checkin.title, "Post-checkin title")
        self.assertEqual(self.checkin.contact_message, "Original message")
        self.assertTrue(any("Message is locked" in warning for warning in outcome.warnings))

    def test_plan_and_destination_always_editable_even_when_locked(self) -> None:
        """The whole point of the feature: the plan stays updatable during an emergency."""
        self._escalate()
        outcome = apply_checkin_edit(
            self.checkin,
            editor=self.profile,
            plan_details="Changed route - heading north instead",
            destination=(44.5, -73.2),
        )

        self.assertEqual(self.checkin.plan_details, "Changed route - heading north instead")
        self.assertEqual(float(self.checkin.destination_latitude), 44.5)
        self.assertTrue(outcome.plan_changed)

    def test_omitted_fields_are_left_untouched(self) -> None:
        """``None`` means "not submitted" - it must never be read as "clear this"."""
        apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Only the plan changed")

        self.assertEqual(self.checkin.title, "Original title")
        self.assertEqual(self.checkin.contact_message, "Original message")

    def test_omitted_fields_produce_no_lock_warnings(self) -> None:
        """A locked check-in edited without touching locked fields warns about nothing."""
        self._escalate()
        outcome = apply_checkin_edit(self.checkin, editor=self.profile, plan_details="New plan only")
        self.assertEqual(outcome.warnings, [])

    def test_explicit_destination_clear(self) -> None:
        apply_checkin_edit(self.checkin, editor=self.profile, destination=(12.0, 34.0))
        apply_checkin_edit(self.checkin, editor=self.profile, destination=(None, None))

        self.assertIsNone(self.checkin.destination_latitude)
        self.assertIsNone(self.checkin.destination_longitude)


class CheckinEditNotifiesContactsTests(_CheckinTestCase):
    """A plan change after escalation re-notifies contacts - and only then."""

    def test_plan_change_after_escalation_notifies(self) -> None:
        """The notification is an ``on_commit`` hook, so the commit has to be simulated.

        A plain ``TestCase`` wraps each test in a transaction that is rolled back
        rather than committed, so ``on_commit`` callbacks never run on their own -
        ``captureOnCommitCallbacks(execute=True)`` is what stands in for the real
        commit here.
        """
        self._escalate()
        with mock.patch("urbanlens.dashboard.services.visits.safety.notify_contacts_of_update") as notify, self.captureOnCommitCallbacks(execute=True):
            apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Detour taken")
        notify.assert_called_once()

    def test_plan_change_before_escalation_does_not_notify(self) -> None:
        with mock.patch("urbanlens.dashboard.services.visits.safety.notify_contacts_of_update") as notify, self.captureOnCommitCallbacks(execute=True):
            apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Detour taken")
        notify.assert_not_called()

    def test_unchanged_plan_does_not_notify(self) -> None:
        """Re-submitting identical text is an autosave no-op, not an alert."""
        self._escalate()
        with mock.patch("urbanlens.dashboard.services.visits.safety.notify_contacts_of_update") as notify, self.captureOnCommitCallbacks(execute=True):
            apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Original plan")
        notify.assert_not_called()

    def test_notification_is_deferred_until_commit(self) -> None:
        """A rolled-back edit must never email real emergency contacts.

        ``captureOnCommitCallbacks`` leaves ``on_commit`` hooks unexecuted unless
        explicitly run, so a notification that fires without it would prove the
        side effect is still inline with the write.
        """
        self._escalate()
        with mock.patch("urbanlens.dashboard.services.visits.safety.notify_contacts_of_update") as notify, self.captureOnCommitCallbacks(execute=False):
            apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Deferred detour")
            notify.assert_not_called()


class CheckinEditArchivedTests(_CheckinTestCase):
    """Defect #1: an edit after archival re-persisted scrubbed plaintext PII.

    ``_scrub_checkin_pii`` blanks the title, plan, message, and destination once
    the encrypted ``SafetyCheckinArchive`` exists. Nothing in the edit path used
    to check for that, so a later autosave wrote fresh plaintext straight back
    onto the scrubbed row - permanently, and outside the encrypted archive.
    """

    def test_edit_is_refused_once_archival_is_scheduled(self) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(archive_scheduled_at=timezone.now())
        self.checkin.refresh_from_db()

        with pytest.raises(CheckinArchivedError):
            apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Should never be written")

    def test_refused_edit_writes_nothing(self) -> None:
        """The refusal must be total - not "some fields got through first"."""
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(archive_scheduled_at=timezone.now(), title="", plan_details="", contact_message="")
        self.checkin.refresh_from_db()

        with pytest.raises(CheckinArchivedError):
            apply_checkin_edit(
                self.checkin,
                editor=self.profile,
                title="Leaked title",
                plan_details="Leaked plan",
                contact_message="Leaked message",
                destination=(1.0, 2.0),
            )

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "")
        self.assertEqual(self.checkin.plan_details, "")
        self.assertEqual(self.checkin.contact_message, "")
        self.assertIsNone(self.checkin.destination_latitude)


class CheckinEditLockRaceTests(_CheckinTestCase):
    """Defect #2: lock flags were read off a stale instance and written back unlocked.

    The edit path used to evaluate ``contacts_locked`` on whatever instance the
    caller had already loaded, then ``save()`` without any row lock. An
    escalation committing in that window (the beat tasks hold no lock of their
    own - see docs/PROBLEMS.md) left the edit free to rewrite the very fields
    contacts had just been emailed about.

    True process-level concurrency isn't reproducible inside a test transaction,
    so the race is simulated at its decisive point: the in-memory instance
    believes the check-in is unlocked while the committed row says otherwise.
    That is precisely the state the old check-and-set produced, and the
    ``select_for_update`` re-fetch is what now catches it.
    """

    def test_escalation_landing_mid_edit_still_freezes_the_title(self) -> None:
        self.assertFalse(self.checkin.contacts_locked)  # the stale view of the world
        self._escalate()  # ...and the committed row, now escalated

        outcome = apply_checkin_edit(self.checkin, editor=self.profile, title="Slipped past the lock")

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "Original title")
        self.assertTrue(any("Title is locked" in warning for warning in outcome.warnings))

    def test_escalation_landing_mid_edit_still_freezes_the_message(self) -> None:
        self.assertFalse(self.checkin.notifications_locked)
        self._escalate()

        outcome = apply_checkin_edit(self.checkin, editor=self.profile, contact_message="Slipped past the lock")

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.contact_message, "Original message")
        self.assertTrue(any("Message is locked" in warning for warning in outcome.warnings))

    def test_plan_edit_still_succeeds_through_a_concurrent_escalation(self) -> None:
        """The fix must not over-correct: the plan is editable at every stage."""
        self._escalate()
        apply_checkin_edit(self.checkin, editor=self.profile, plan_details="Still updatable")

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.plan_details, "Still updatable")


class ResolveContactInputsTests(TestCase):
    """A username may only name an existing connection."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.stranger = Profile.objects.get(user=baker.make(User, username="stranger"))

    def test_non_connection_username_is_rejected(self) -> None:
        """Otherwise an API key could nominate arbitrary accounts as emergency contacts."""
        with mock.patch("urbanlens.dashboard.services.social.connections.get_connections", return_value=[]):
            inputs, rejected = resolve_contact_inputs(self.profile, [{"username": "stranger"}])

        self.assertEqual(inputs, [])
        self.assertTrue(any("isn't one of your connections" in message for message in rejected))

    def test_connection_username_is_accepted(self) -> None:
        with mock.patch("urbanlens.dashboard.services.social.connections.get_connections", return_value=[self.stranger]):
            inputs, rejected = resolve_contact_inputs(self.profile, [{"username": "stranger"}])

        self.assertEqual(rejected, [])
        self.assertEqual(inputs, [(self.stranger, None, "stranger")])

    def test_email_contact_is_normalized(self) -> None:
        inputs, rejected = resolve_contact_inputs(self.profile, [{"email": "Someone@Example.COM", "name": "Someone"}])

        self.assertEqual(rejected, [])
        self.assertEqual(inputs, [(None, "someone@example.com", "Someone")])
