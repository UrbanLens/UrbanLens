"""Tests for the safety check-in lifecycle and the widened VisitSuggestion origin constraint."""

from __future__ import annotations

import datetime
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import (
    EmergencyContactDefault,
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinMessage,
    SafetyCheckinStatus,
    SafetyContactOptOut,
    SafetyContactOptOutScope,
)
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.services.visits.safety import (
    cancel_checkin,
    check_in,
    create_checkin,
    escalate_checkin,
    get_active_checkin,
    get_active_checkins,
    is_contact_opted_out,
    mark_found_safe,
    record_contact_opt_out,
)


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
        from urbanlens.dashboard.services.visits.safety import _conclude_checkin

        _conclude_checkin(checkin)

        self.assertEqual(VisitSuggestion.objects.filter(safety_checkin=checkin).count(), 1)

    def test_escalate_checkin_notifies_contacts_without_resolving(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.AWAITING_CHECKIN)
        contact_profile = baker.make("auth.User").profile
        contact = baker.make(
            "dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=contact_profile, email=None
        )

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
        finder = baker.make(
            "dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=finder_profile, email=None
        )
        baker.make("dashboard.SafetyCheckinContact", checkin=checkin, contact_profile=other_profile, email=None)

        mark_found_safe(finder)

        checkin.refresh_from_db()
        finder.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertIsNotNone(finder.found_safe_at)
        self.assertEqual(VisitSuggestion.objects.filter(safety_checkin=checkin).count(), 1)

    def test_mark_found_safe_does_not_re_resolve_an_already_resolved_checkin(self):
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.CHECKED_IN, resolved_at=timezone.now())
        contact = baker.make(
            "dashboard.SafetyCheckinContact",
            checkin=checkin,
            contact_profile=baker.make("auth.User").profile,
            email=None,
        )

        mark_found_safe(contact)

        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.CHECKED_IN)
        # Regression guard: a no-op resolution must not still post a "marked safe" system
        # chat message - a stale mark-safe link hit after resolution (or after archival,
        # which only ever happens once already-resolved) must be a true no-op.
        self.assertEqual(SafetyCheckinMessage.objects.filter(checkin=checkin).count(), 0)

    def test_two_contacts_racing_to_mark_safe_only_resolve_once(self):
        """Regression guard: the resolution guard is a conditional UPDATE, not an
        in-memory `is_resolved` read-then-write - two contacts (or a contact and a
        partner) reporting the same check-in safe at nearly the same moment must not
        both pass, which would double-notify everyone and double-schedule archival.
        """
        checkin = _checkin(self.profile, status=SafetyCheckinStatus.OVERDUE)
        first = baker.make(
            "dashboard.SafetyCheckinContact",
            checkin=checkin,
            contact_profile=baker.make("auth.User").profile,
            email=None,
        )
        second = baker.make(
            "dashboard.SafetyCheckinContact",
            checkin=checkin,
            contact_profile=baker.make("auth.User").profile,
            email=None,
        )
        # Both handlers loaded the same pre-resolution `checkin` row before either wrote -
        # mirrors two concurrent requests each holding their own stale in-memory copy.
        first.checkin.refresh_from_db()
        second.checkin.refresh_from_db()

        mark_found_safe(first)
        mark_found_safe(second)

        checkin.refresh_from_db()
        self.assertEqual(checkin.resolved_by_label, first.display_name)
        self.assertEqual(VisitSuggestion.objects.filter(safety_checkin=checkin).count(), 1)
        # The system chat message is posted only by whichever call actually resolves the
        # checkin - the loser of the race must not also post its own "marked safe" message.
        self.assertEqual(SafetyCheckinMessage.objects.filter(checkin=checkin).count(), 1)


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
        suggestion = baker.make(
            "dashboard.VisitSuggestion",
            origin_visit=None,
            trip_activity=None,
            safety_checkin=checkin,
            **self._base_kwargs(),
        )
        self.assertEqual(suggestion.safety_checkin_id, checkin.pk)

    def test_no_origin_violates_constraint(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            baker.make(
                "dashboard.VisitSuggestion",
                origin_visit=None,
                trip_activity=None,
                safety_checkin=None,
                **self._base_kwargs(),
            )

    def test_two_origins_violates_constraint(self):
        checkin = _checkin(self.suggested_to)
        pin = baker.make("dashboard.Pin", profile=self.suggested_to)
        visit = baker.make("dashboard.PinVisit", pin=pin)
        with pytest.raises(IntegrityError), transaction.atomic():
            baker.make(
                "dashboard.VisitSuggestion",
                origin_visit=visit,
                trip_activity=None,
                safety_checkin=checkin,
                **self._base_kwargs(),
            )


class SafetyCheckinQuerySetTests(TestCase):
    """due_for_reminder()/overdue() boundary conditions."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_due_for_reminder_only_includes_scheduled_past_due(self):
        due = _checkin(
            self.profile,
            status=SafetyCheckinStatus.SCHEDULED,
            checkin_by=timezone.now() - datetime.timedelta(minutes=1),
        )
        not_yet = _checkin(
            self.profile, status=SafetyCheckinStatus.SCHEDULED, checkin_by=timezone.now() + datetime.timedelta(hours=1)
        )
        past_grace = _checkin(
            self.profile,
            status=SafetyCheckinStatus.SCHEDULED,
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        already_reminded = _checkin(
            self.profile,
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            checkin_by=timezone.now() - datetime.timedelta(minutes=1),
        )

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
        contact = baker.make(
            "dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None
        )
        self.assertEqual(SafetyCheckinContact.objects.by_token(contact.token).first(), contact)

    def test_empty_for_an_unknown_token(self):
        self.assertFalse(SafetyCheckinContact.objects.by_token(uuid4()).exists())

    def test_chains_with_select_related(self):
        """Every real call site chains select_related(...) before by_token() -
        confirm that composition still resolves to exactly the right row."""
        contact = baker.make(
            "dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None
        )
        result = (
            SafetyCheckinContact.objects.select_related("checkin", "checkin__profile").by_token(contact.token).first()
        )
        self.assertEqual(result, contact)
        self.assertEqual(result.checkin_id, self.checkin.pk)


class SafetyContactPortalEscalationGateTests(TestCase):
    """The token contact portal (and its markup JSON) must not disclose the plan, message,
    route, or photos before the check-in has actually escalated - the token is only ever
    emailed at escalation, but nothing previously stopped a leaked/guessed/forwarded token
    from returning the full plan regardless of check-in state. See docs/audits/GOALS_CODE_AUDIT.md
    ("Safety check-ins")."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.checkin = _checkin(
            self.profile,
            plan_details="Meet at the north gate, follow the fence line.",
            contact_message="Call the ranger station if I'm late.",
        )
        self.contact = baker.make(
            "dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None
        )

    def _escalate(self):
        self.checkin.escalated_at = timezone.now()
        self.checkin.save(update_fields=["escalated_at", "updated"])

    def test_portal_hides_the_plan_and_message_before_escalation(self):
        response = self.client.get(reverse("safety.contact.portal", kwargs={"token": self.contact.token}))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("Meet at the north gate", content)
        self.assertNotIn("Call the ranger station", content)

    def test_portal_shows_the_plan_and_message_once_escalated(self):
        self._escalate()

        response = self.client.get(reverse("safety.contact.portal", kwargs={"token": self.contact.token}))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Meet at the north gate", content)
        self.assertIn("Call the ranger station", content)

    def test_portal_hides_photos_before_escalation(self):
        baker.make(
            "dashboard.Image", safety_checkin=self.checkin, profile=self.profile, image="checkin_photos/trailhead.jpg"
        )

        response = self.client.get(reverse("safety.contact.portal", kwargs={"token": self.contact.token}))

        self.assertNotIn("safety-photo-thumb", response.content.decode())

    def test_portal_shows_photos_once_escalated(self):
        baker.make(
            "dashboard.Image", safety_checkin=self.checkin, profile=self.profile, image="checkin_photos/trailhead.jpg"
        )
        self._escalate()

        response = self.client.get(reverse("safety.contact.portal", kwargs={"token": self.contact.token}))

        self.assertIn("safety-photo-thumb", response.content.decode())

    def test_markup_json_is_empty_before_escalation(self):
        markup_map = baker.make("dashboard.MarkupMap", profile=self.profile)
        self.checkin.markup_map = markup_map
        self.checkin.save(update_fields=["markup_map", "updated"])
        baker.make(
            "dashboard.PinMarkup",
            parent_map=markup_map,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )

        response = self.client.get(reverse("safety.contact.markup.json", kwargs={"token": self.contact.token}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["markup_items"], [])

    def test_markup_json_returns_items_once_escalated(self):
        markup_map = baker.make("dashboard.MarkupMap", profile=self.profile)
        self.checkin.markup_map = markup_map
        self.checkin.save(update_fields=["markup_map", "updated"])
        baker.make(
            "dashboard.PinMarkup",
            parent_map=markup_map,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )
        self._escalate()

        response = self.client.get(reverse("safety.contact.markup.json", kwargs={"token": self.contact.token}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["markup_items"]), 1)

    def test_portal_is_a_404_for_an_unknown_token(self):
        self.assertEqual(self.client.get(reverse("safety.contact.portal", kwargs={"token": uuid4()})).status_code, 404)

    def test_markup_json_is_a_404_for_an_unknown_token(self):
        self.assertEqual(
            self.client.get(reverse("safety.contact.markup.json", kwargs={"token": uuid4()})).status_code, 404
        )


class OneActiveCheckinAtATimeTests(TestCase):
    """create_checkin()/get_active_checkin() enforce a single active check-in per profile."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_get_active_checkin_is_none_with_no_checkins(self):
        self.assertIsNone(get_active_checkin(self.profile))

    def test_get_active_checkin_returns_the_unresolved_checkin(self):
        checkin = create_checkin(
            profile=self.profile,
            title="Ridge Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        self.assertEqual(get_active_checkin(self.profile), checkin)

    def test_create_checkin_rejects_a_second_active_checkin(self):
        create_checkin(
            profile=self.profile,
            title="Ridge Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )

        with pytest.raises(ValueError):
            create_checkin(
                profile=self.profile,
                title="Another Hike",
                checkin_by=timezone.now() + datetime.timedelta(hours=3),
                grace_period=datetime.timedelta(hours=1),
            )

        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile).count(), 1)

    def test_create_checkin_allowed_again_once_prior_is_resolved(self):
        first = create_checkin(
            profile=self.profile,
            title="Ridge Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        cancel_checkin(first)

        second = create_checkin(
            profile=self.profile,
            title="Another Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
        )

        self.assertEqual(get_active_checkin(self.profile), second)
        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile).count(), 2)
        self.assertNotEqual(first.pk, second.pk)


class TripScopedActiveCheckinTests(TestCase):
    """A profile may have one active check-in per (profile, trip) scope - not just one, period."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.trip = baker.make("dashboard.Trip", creator=self.profile)
        self.other_trip = baker.make("dashboard.Trip", creator=self.profile)

    def test_general_and_trip_scoped_checkins_can_both_be_active(self):
        general = create_checkin(
            profile=self.profile,
            title="General",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        trip_checkin = create_checkin(
            profile=self.profile,
            title="Trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            trip=self.trip,
        )

        self.assertEqual(get_active_checkin(self.profile, trip=None), general)
        self.assertEqual(get_active_checkin(self.profile, trip=self.trip), trip_checkin)

    def test_two_different_trips_can_both_have_active_checkins(self):
        first = create_checkin(
            profile=self.profile,
            title="Trip A",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            trip=self.trip,
        )
        second = create_checkin(
            profile=self.profile,
            title="Trip B",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            trip=self.other_trip,
        )

        self.assertEqual(get_active_checkin(self.profile, trip=self.trip), first)
        self.assertEqual(get_active_checkin(self.profile, trip=self.other_trip), second)

    def test_create_checkin_rejects_a_second_active_checkin_for_the_same_trip(self):
        create_checkin(
            profile=self.profile,
            title="First",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            trip=self.trip,
        )

        with pytest.raises(ValueError):
            create_checkin(
                profile=self.profile,
                title="Second",
                checkin_by=timezone.now() + datetime.timedelta(hours=3),
                grace_period=datetime.timedelta(hours=1),
                trip=self.trip,
            )

        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile, trip=self.trip).count(), 1)

    def test_get_active_checkins_returns_every_scope(self):
        general = create_checkin(
            profile=self.profile,
            title="General",
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
        )
        trip_checkin = create_checkin(
            profile=self.profile,
            title="Trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=1),
            grace_period=datetime.timedelta(hours=1),
            trip=self.trip,
        )

        self.assertEqual(list(get_active_checkins(self.profile)), [trip_checkin, general])

    def test_get_active_checkins_excludes_resolved(self):
        checkin = create_checkin(
            profile=self.profile,
            title="Trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            trip=self.trip,
        )
        cancel_checkin(checkin)

        self.assertEqual(list(get_active_checkins(self.profile)), [])


class EmergencyContactDefaultQuerySetTests(TestCase):
    """EmergencyContactDefault.objects.for_owner() - two call sites in services/safety.py
    (save_contact_defaults, default_contacts_as_input) each re-wrote
    `EmergencyContactDefault.objects.filter(owner=profile)` directly."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.other_profile = baker.make("auth.User").profile

    def test_returns_only_the_owners_defaults(self):
        mine = baker.make(
            "dashboard.EmergencyContactDefault", owner=self.profile, email="a@example.com", contact_profile=None
        )
        baker.make(
            "dashboard.EmergencyContactDefault", owner=self.other_profile, email="b@example.com", contact_profile=None
        )
        self.assertEqual(list(EmergencyContactDefault.objects.for_owner(self.profile)), [mine])

    def test_empty_for_a_profile_with_no_defaults(self):
        self.assertFalse(EmergencyContactDefault.objects.for_owner(self.profile).exists())

    def test_respects_default_order(self):
        second = baker.make(
            "dashboard.EmergencyContactDefault",
            owner=self.profile,
            email="second@example.com",
            contact_profile=None,
            order=1,
        )
        first = baker.make(
            "dashboard.EmergencyContactDefault",
            owner=self.profile,
            email="first@example.com",
            contact_profile=None,
            order=0,
        )
        self.assertEqual(list(EmergencyContactDefault.objects.for_owner(self.profile)), [first, second])


class SafetyContactOptOutBlocksNotificationTests(TestCase):
    """SafetyContactOptOut.objects.blocks_notification()/is_contact_opted_out() - the only
    call site previously built the identity/scope Q-object query inline in services/safety.py;
    is_contact_opted_out() now delegates to the manager method."""

    def setUp(self):
        self.owner = baker.make("auth.User").profile
        self.other_owner = baker.make("auth.User").profile
        self.checkin = _checkin(self.owner)
        self.other_checkin = _checkin(self.owner)

    def test_no_opt_out_allows_notification(self):
        self.assertFalse(is_contact_opted_out(None, "contact@example.com", owner=self.owner))

    def test_global_opt_out_blocks_by_email_regardless_of_owner(self):
        baker.make(
            "dashboard.SafetyContactOptOut",
            email="contact@example.com",
            scope=SafetyContactOptOutScope.GLOBAL,
            owner=None,
            checkin=None,
            contact_profile=None,
        )
        self.assertTrue(is_contact_opted_out(None, "contact@example.com", owner=self.owner))
        self.assertTrue(is_contact_opted_out(None, "contact@example.com", owner=self.other_owner))

    def test_owner_scoped_opt_out_blocks_only_that_owner(self):
        baker.make(
            "dashboard.SafetyContactOptOut",
            email="contact@example.com",
            scope=SafetyContactOptOutScope.OWNER,
            owner=self.owner,
            checkin=None,
            contact_profile=None,
        )
        self.assertTrue(is_contact_opted_out(None, "contact@example.com", owner=self.owner))
        self.assertFalse(is_contact_opted_out(None, "contact@example.com", owner=self.other_owner))

    def test_checkin_scoped_opt_out_requires_the_matching_checkin(self):
        baker.make(
            "dashboard.SafetyContactOptOut",
            email="contact@example.com",
            scope=SafetyContactOptOutScope.CHECKIN,
            owner=None,
            checkin=self.checkin,
            contact_profile=None,
        )
        self.assertTrue(is_contact_opted_out(None, "contact@example.com", owner=self.owner, checkin=self.checkin))
        self.assertFalse(
            is_contact_opted_out(None, "contact@example.com", owner=self.owner, checkin=self.other_checkin)
        )
        self.assertFalse(is_contact_opted_out(None, "contact@example.com", owner=self.owner))

    def test_matches_by_contact_profile_not_email(self):
        contact_profile = baker.make("auth.User").profile
        baker.make(
            "dashboard.SafetyContactOptOut",
            email=None,
            scope=SafetyContactOptOutScope.GLOBAL,
            owner=None,
            checkin=None,
            contact_profile=contact_profile,
        )
        self.assertTrue(is_contact_opted_out(contact_profile, None, owner=self.owner))

    def test_email_match_is_case_insensitive(self):
        baker.make(
            "dashboard.SafetyContactOptOut",
            email="Contact@Example.com",
            scope=SafetyContactOptOutScope.GLOBAL,
            owner=None,
            checkin=None,
            contact_profile=None,
        )
        self.assertTrue(is_contact_opted_out(None, "contact@example.com", owner=self.owner))


class RecordContactOptOutDedupTests(TestCase):
    """record_contact_opt_out's docstring promises repeat clicks (or an email client's
    link-scanner prefetching the confirm GET) don't create duplicate rows - previously
    unenforced at the DB level, so a get_or_create race could insert two."""

    def setUp(self):
        self.owner = baker.make("auth.User").profile
        self.checkin = _checkin(self.owner)
        self.contact = baker.make(
            "dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None
        )

    def test_repeat_calls_do_not_duplicate_a_checkin_scoped_opt_out(self):
        record_contact_opt_out(self.contact, SafetyContactOptOutScope.CHECKIN)
        record_contact_opt_out(self.contact, SafetyContactOptOutScope.CHECKIN)

        self.assertEqual(
            SafetyContactOptOut.objects.filter(
                email="contact@example.com", scope=SafetyContactOptOutScope.CHECKIN, checkin=self.checkin
            ).count(),
            1,
        )

    def test_a_second_identical_row_is_rejected_at_the_database(self) -> None:
        """Direct proof the constraint - not just the service function's own call
        pattern - is what prevents the duplicate."""
        record_contact_opt_out(self.contact, SafetyContactOptOutScope.CHECKIN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SafetyContactOptOut.objects.create(
                contact_profile=None,
                email="contact@example.com",
                scope=SafetyContactOptOutScope.CHECKIN,
                owner=None,
                checkin=self.checkin,
            )
