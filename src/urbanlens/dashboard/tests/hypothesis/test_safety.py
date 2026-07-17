"""Tests for the safety check-in lifecycle and the widened VisitSuggestion origin constraint."""

from __future__ import annotations

import datetime
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyCheckinStatus
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.services.safety import cancel_checkin, check_in, create_checkin, escalate_checkin, get_active_checkin, mark_found_safe


def _checkin(profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Test hike",
        "checkin_by": timezone.now() - datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
        "destination_latitude": "40.000000",
        "destination_longitude": "-74.000000",
    }
    defaults.update(kwargs)
    return baker.make("dashboard.SafetyCheckin", **defaults)


class SafetyCheckinLifecycleTests(TestCase):
    """check_in()/escalate_checkin()/mark_found_safe() transitions and _conclude_checkin idempotency."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_check_in_resolves_and_creates_visit_suggestion(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.AWAITING_CHECKIN)

        check_in(checkin, self.profile)

        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)
        self.assertIsNotNone(checkin.resolved_at)
        self.assertEqual(VisitSuggestion.objects.filter(safety_checkin=checkin, suggested_to=self.profile).count(), 1)

    def test_check_in_is_idempotent_about_visit_suggestion(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.AWAITING_CHECKIN)
        check_in(checkin, self.profile)

        # A second conclusion attempt (e.g. a stray double-submit) must not raise
        # the exactly-one-origin constraint by creating a duplicate suggestion.
        from urbanlens.dashboard.services.safety import _conclude_checkin

        _conclude_checkin(checkin)

        self.assertEqual(VisitSuggestion.objects.filter(safety_checkin=checkin).count(), 1)

    def test_escalate_checkin_notifies_contacts_without_resolving(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.AWAITING_CHECKIN)
        contact_profile = baker.make("auth.User").profile
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=contact_profile, email=None)

        escalate_checkin(checkin)

        checkin.refresh_from_db()
        contact.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.OVERDUE)
        self.assertIsNotNone(checkin.escalated_at)
        self.assertFalse(checkin.is_resolved)
        self.assertIsNotNone(contact.notified_at)

    def test_mark_found_safe_resolves_checkin_and_notifies_other_contacts(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.OVERDUE)
        finder_profile = baker.make("auth.User").profile
        other_profile = baker.make("auth.User").profile
        finder = baker.make("dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=finder_profile, email=None)
        baker.make("dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=other_profile, email=None)

        mark_found_safe(finder)

        checkin.refresh_from_db()
        finder.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertIsNotNone(finder.found_safe_at)
        self.assertEqual(VisitSuggestion.objects.filter(safety_checkin=checkin).count(), 1)

    def test_mark_found_safe_does_not_re_resolve_an_already_resolved_checkin(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.CHECKED_IN, resolved_at=timezone.now())
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=baker.make("auth.User").profile, email=None)

        mark_found_safe(contact)

        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)


class VisitSuggestionOriginConstraintTests(TestCase):
    """The exactly-one-of-three-origins CheckConstraint on VisitSuggestion."""

    def setUp(self):
        self.suggested_to = baker.make("auth.User").profile

    def _base_kwargs(self):
        return {
            "suggested_to": self.suggested_to,
            "latitude": "40.000000",
            "longitude": "-74.000000",
            "visited_at": timezone.now(),
        }

    def test_safety_checkin_origin_alone_is_valid(self):
        checkin = _checkin(self.suggested_to)
        suggestion = baker.make("dashboard.VisitSuggestion", origin_visit=None, trip_activity=None, safety_checkin=checkin, **self._base_kwargs())
        self.assertEqual(suggestion.safety_checkin_id, checkin.pk)

    def test_no_origin_violates_constraint(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            baker.make("dashboard.VisitSuggestion", origin_visit=None, trip_activity=None, safety_checkin=None, **self._base_kwargs())

    def test_two_origins_violates_constraint(self):
        checkin = _checkin(self.suggested_to)
        pin = baker.make("dashboard.Pin", profile=self.suggested_to)
        visit = baker.make("dashboard.PinVisit", pin=pin)
        with pytest.raises(IntegrityError), transaction.atomic():
            baker.make("dashboard.VisitSuggestion", origin_visit=visit, trip_activity=None, safety_checkin=checkin, **self._base_kwargs())


class SafetyCheckinQuerySetTests(TestCase):
    """due_for_reminder()/overdue() boundary conditions."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_due_for_reminder_only_includes_scheduled_past_due(self):
        due = _checkin(self.profile, status=SafetyCheckinStatus.SCHEDULED, checkin_by=timezone.now() - datetime.timedelta(minutes=1))
        not_yet = _checkin(self.profile, status=SafetyCheckinStatus.SCHEDULED, checkin_by=timezone.now() + datetime.timedelta(hours=1))
        past_grace = _checkin(
            self.profile,
            status=SafetyCheckinStatus.SCHEDULED,
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        already_reminded = _checkin(self.profile, status=SafetyCheckinStatus.AWAITING_CHECKIN, checkin_by=timezone.now() - datetime.timedelta(minutes=1))

        results = set(SafetyCheckin.objects.due_for_reminder().values_list("pk", flat=True))

        self.assertIn(due.pk, results)
        self.assertNotIn(not_yet.pk, results)
        self.assertNotIn(past_grace.pk, results)
        self.assertNotIn(already_reminded.pk, results)

    def test_overdue_includes_unreminded_scheduled_checkins_past_grace_period(self):
        overdue = _checkin(
            self.profile,
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        unreminded_overdue = _checkin(
            self.profile,
            status=SafetyCheckinStatus.SCHEDULED,
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        within_grace = _checkin(
            self.profile,
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            checkin_by=timezone.now() - datetime.timedelta(minutes=10),
            grace_period=datetime.timedelta(hours=1),
        )

        results = set(SafetyCheckin.objects.overdue().values_list("pk", flat=True))

        self.assertIn(overdue.pk, results)
        self.assertIn(unreminded_overdue.pk, results)
        self.assertNotIn(within_grace.pk, results)

    def test_active_excludes_only_resolved_statuses(self):
        scheduled = _checkin(self.profile, status=SafetyCheckinStatus.SCHEDULED)
        awaiting = _checkin(self.profile, status=SafetyCheckinStatus.AWAITING_CHECKIN)
        overdue = _checkin(self.profile, status=SafetyCheckinStatus.OVERDUE)
        checked_in = _checkin(self.profile, status=SafetyCheckinStatus.CHECKED_IN)
        found_safe = _checkin(self.profile, status=SafetyCheckinStatus.FOUND_SAFE)
        cancelled = _checkin(self.profile, status=SafetyCheckinStatus.CANCELLED)

        results = set(SafetyCheckin.objects.active().values_list("pk", flat=True))

        self.assertEqual(results, {scheduled.pk, awaiting.pk, overdue.pk})
        self.assertNotIn(checked_in.pk, results)
        self.assertNotIn(found_safe.pk, results)
        self.assertNotIn(cancelled.pk, results)


class SafetyCheckinContactByTokenTests(TestCase):
    """SafetyCheckinContact.objects.by_token() - previously six call sites
    across controllers/markup.py and controllers/safety.py each re-wrote
    `get_object_or_404(SafetyCheckinContact[.objects...], token=token)` directly."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.checkin = _checkin(self.profile)

    def test_returns_the_matching_contact(self):
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None)
        self.assertEqual(SafetyCheckinContact.objects.by_token(contact.token).first(), contact)

    def test_empty_for_an_unknown_token(self):
        self.assertFalse(SafetyCheckinContact.objects.by_token(uuid4()).exists())

    def test_chains_with_select_related(self):
        """Every real call site chains select_related(...) before by_token() -
        confirm that composition still resolves to exactly the right row."""
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None)
        result = SafetyCheckinContact.objects.select_related("checkin", "checkin__profile").by_token(contact.token).first()
        self.assertEqual(result, contact)
        self.assertEqual(result.checkin_id, self.checkin.pk)


class OneActiveCheckinAtATimeTests(TestCase):
    """create_checkin()/get_active_checkin() enforce a single active check-in per profile."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_get_active_checkin_is_none_with_no_checkins(self):
        self.assertIsNone(get_active_checkin(self.profile))

    def test_get_active_checkin_returns_the_unresolved_checkin(self):
        checkin = create_checkin(profile=self.profile, title="Ridge Hike", checkin_by=timezone.now() + datetime.timedelta(hours=2), grace_period=datetime.timedelta(hours=1))
        self.assertEqual(get_active_checkin(self.profile), checkin)

    def test_create_checkin_rejects_a_second_active_checkin(self):
        create_checkin(profile=self.profile, title="Ridge Hike", checkin_by=timezone.now() + datetime.timedelta(hours=2), grace_period=datetime.timedelta(hours=1))

        with pytest.raises(ValueError):
            create_checkin(profile=self.profile, title="Another Hike", checkin_by=timezone.now() + datetime.timedelta(hours=3), grace_period=datetime.timedelta(hours=1))

        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile).count(), 1)

    def test_create_checkin_allowed_again_once_prior_is_resolved(self):
        first = create_checkin(profile=self.profile, title="Ridge Hike", checkin_by=timezone.now() + datetime.timedelta(hours=2), grace_period=datetime.timedelta(hours=1))
        cancel_checkin(first)

        second = create_checkin(profile=self.profile, title="Another Hike", checkin_by=timezone.now() + datetime.timedelta(hours=3), grace_period=datetime.timedelta(hours=1))

        self.assertEqual(get_active_checkin(self.profile), second)
        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile).count(), 2)
        self.assertNotEqual(first.pk, second.pk)
