"""Tests for the visit-suggestion feature: services/visits.py business logic.

Covers:
- build_visit_suggestion_message: privacy-safe place description fallback chain.
- find_pin_at / get_or_create_pin_at: pin dedup by location id or coordinates.
- create_visit_suggestion: suggestion/notification creation, the "nothing would
  change so don't notify" skip, and the existing-visit merge-offer path.
- accept_visit_suggestion / merge_visit_suggestion: pin creation, participant
  mutual-connection filtering, and VisitSource selection.
- TripActivityCompleteView: completer auto-logs immediately, other rsvp=yes
  members get suggestions, rsvp=no/maybe/None members get nothing.
"""
from __future__ import annotations

import datetime

from django.test import Client
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits import (
    accept_visit_suggestion,
    build_visit_suggestion_message,
    create_visit_suggestion,
    find_pin_at,
    get_or_create_pin_at,
    merge_visit_suggestion,
)

_hyp = hyp_settings(max_examples=30, deadline=None)


def _make_accepted_friendship(a, b) -> Friendship:
    return Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


# ---------------------------------------------------------------------------
# build_visit_suggestion_message
# ---------------------------------------------------------------------------

class BuildVisitSuggestionMessageTests(TestCase):
    """Fallback chain: official_name -> name -> city+state -> city -> generic."""

    def test_no_location_returns_generic(self) -> None:
        self.assertEqual(build_visit_suggestion_message(None), "at a location")

    def test_meaningful_official_name_is_used(self) -> None:
        location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name="Old Mill", name="Unnamed Location")
        self.assertEqual(build_visit_suggestion_message(location), "at Old Mill")

    def test_falls_back_to_name_when_official_name_not_meaningful(self) -> None:
        location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name="N/A", name="Riverside Mill")
        self.assertEqual(build_visit_suggestion_message(location), "at Riverside Mill")

    def test_falls_back_to_city_and_state(self) -> None:
        location = baker.make(
            Location,
            latitude="40.0",
            longitude="-74.0",
            official_name=None,
            name="Unnamed Location",
            locality="Springfield",
            administrative_area_level_1="IL",
        )
        self.assertEqual(build_visit_suggestion_message(location), "in Springfield, IL")

    def test_falls_back_to_city_only(self) -> None:
        location = baker.make(
            Location,
            latitude="40.0",
            longitude="-74.0",
            official_name=None,
            name="Unnamed Location",
            locality="Springfield",
            administrative_area_level_1=None,
        )
        self.assertEqual(build_visit_suggestion_message(location), "in Springfield")

    def test_generic_when_nothing_usable(self) -> None:
        location = baker.make(
            Location,
            latitude="40.0",
            longitude="-74.0",
            official_name=None,
            name="Unnamed Location",
            locality=None,
            administrative_area_level_1=None,
        )
        self.assertEqual(build_visit_suggestion_message(location), "at a location")

    @given(sentinel=st.text(alphabet=st.characters(max_codepoint=127, whitelist_categories=("Lu", "Ll")), min_size=5, max_size=20))
    @_hyp
    def test_never_reads_pin_or_visit_fields(self, sentinel: str) -> None:
        """The function only accepts a Location, so it structurally cannot leak Pin/PinVisit data."""
        location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name=sentinel)
        self.assertIn(sentinel, build_visit_suggestion_message(location))


class BuildVisitSuggestionMessageOriginPinFallbackTests(TestCase):
    """When there's no Location (e.g. a private, unlinked pin), fall back to the pin's own official_name/city/state."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def test_falls_back_to_pin_official_name_when_no_location(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=None, is_private=True, official_name="Old Mill", name="my secret spot")
        self.assertEqual(build_visit_suggestion_message(None, origin_pin=pin), "at Old Mill")

    def test_falls_back_to_pin_city_state_when_no_official_name(self) -> None:
        pin = baker.make(
            Pin,
            profile=self.profile,
            location=None,
            is_private=True,
            official_name=None,
            locality="Springfield",
            administrative_area_level_1="IL",
        )
        self.assertEqual(build_visit_suggestion_message(None, origin_pin=pin), "in Springfield, IL")

    def test_never_falls_back_to_pins_private_name(self) -> None:
        """Pin.name is the user's private custom label and must never appear in the message."""
        pin = baker.make(Pin, profile=self.profile, location=None, is_private=True, official_name=None, name="TOTALLY-SECRET-PIN-NAME", locality=None, administrative_area_level_1=None)
        self.assertEqual(build_visit_suggestion_message(None, origin_pin=pin), "at a location")

    def test_location_takes_priority_over_origin_pin_fallback(self) -> None:
        location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name="Location Wins")
        pin = baker.make(Pin, profile=self.profile, location=location, official_name="Pin Name Ignored")
        self.assertEqual(build_visit_suggestion_message(location, origin_pin=pin), "at Location Wins")


# ---------------------------------------------------------------------------
# find_pin_at / get_or_create_pin_at
# ---------------------------------------------------------------------------

class FindPinAtTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0")

    def test_matches_by_location_id(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=self.location, latitude=None, longitude=None)
        found = find_pin_at(self.profile, location_id=self.location.pk, latitude=99.0, longitude=99.0)
        self.assertEqual(found, pin)

    def test_matches_by_coordinates_when_no_location(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=None, latitude="41.5", longitude="-75.5")
        found = find_pin_at(self.profile, location_id=None, latitude=41.5, longitude=-75.5)
        self.assertEqual(found, pin)

    def test_returns_none_when_no_match(self) -> None:
        found = find_pin_at(self.profile, location_id=999999, latitude=1.0, longitude=1.0)
        self.assertIsNone(found)

    def test_excludes_detail_pins(self) -> None:
        parent = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None, parent_location=None)
        baker.make(Pin, profile=self.profile, location=self.location, parent_pin=parent)
        found = find_pin_at(self.profile, location_id=self.location.pk)
        self.assertEqual(found, parent)


class GetOrCreatePinAtTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0")

    def test_reuses_existing_pin(self) -> None:
        existing = baker.make(Pin, profile=self.profile, location=self.location)
        pin = get_or_create_pin_at(self.profile, location=self.location, latitude=40.0, longitude=-74.0)
        self.assertEqual(pin, existing)
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 1)

    def test_creates_minimal_pin_with_no_private_data(self) -> None:
        pin = get_or_create_pin_at(self.profile, location=self.location, latitude=40.0, longitude=-74.0)
        self.assertIsNone(pin.name)
        self.assertFalse(pin.name_is_user_provided)
        self.assertEqual(pin.location_id, self.location.pk)


# ---------------------------------------------------------------------------
# create_visit_suggestion
# ---------------------------------------------------------------------------

class CreateVisitSuggestionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.suggester = baker.make("auth.User").profile
        self.recipient = baker.make("auth.User").profile
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name="Old Mill")
        self.visited_at = timezone.make_aware(datetime.datetime(2026, 6, 1, 14, 0))

    def test_creates_suggestion_and_notification_when_no_existing_visit(self) -> None:
        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)

        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=origin_visit,
        )

        self.assertIsNotNone(suggestion)
        self.assertIsNotNone(suggestion.notification_id)
        self.assertEqual(suggestion.status, VisitSuggestionStatus.PENDING)
        self.assertFalse(suggestion.offers_merge)

    def test_uses_origin_pins_official_name_when_pin_has_no_location(self) -> None:
        """A private, unlinked origin pin still has a useful official_name to fall back to."""
        origin_pin = baker.make(Pin, profile=self.suggester, location=None, is_private=True, official_name="Secret Warehouse")
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)

        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=None,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=origin_visit,
            origin_pin=origin_pin,
        )

        self.assertIn("Secret Warehouse", suggestion.notification.message)

    def test_notification_never_contains_private_pin_data(self) -> None:
        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location, name="TOTALLY-SECRET-PIN-NAME")
        origin_visit = baker.make(
            PinVisit,
            pin=origin_pin,
            visited_at=self.visited_at,
            source=VisitSource.MANUAL,
            notes="TOTALLY-SECRET-VISIT-NOTES",
        )

        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=origin_visit,
        )

        notification = suggestion.notification
        self.assertNotIn("TOTALLY-SECRET-PIN-NAME", notification.title)
        self.assertNotIn("TOTALLY-SECRET-PIN-NAME", notification.message)
        self.assertNotIn("TOTALLY-SECRET-VISIT-NOTES", notification.title)
        self.assertNotIn("TOTALLY-SECRET-VISIT-NOTES", notification.message)

    def test_no_notification_created_when_recipient_opted_out(self) -> None:
        baker.make(NotificationPreference, profile=self.recipient, visit_suggested=DeliveryPreference.NONE)
        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)

        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=origin_visit,
        )

        self.assertIsNotNone(suggestion)
        self.assertIsNone(suggestion.notification_id)

    def test_skips_entirely_when_existing_visit_has_all_participants_already(self) -> None:
        _make_accepted_friendship(self.recipient, self.suggester)
        recipient_pin = baker.make(Pin, profile=self.recipient, location=self.location)
        existing_visit = baker.make(PinVisit, pin=recipient_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)
        existing_visit.participants.add(self.suggester)

        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)

        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=origin_visit,
        )

        self.assertIsNone(suggestion)
        self.assertFalse(VisitSuggestion.objects.exists())

    def test_offers_merge_when_existing_visit_would_gain_a_participant(self) -> None:
        _make_accepted_friendship(self.recipient, self.suggester)
        recipient_pin = baker.make(Pin, profile=self.recipient, location=self.location)
        existing_visit = baker.make(PinVisit, pin=recipient_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)
        # No participants yet - accepting would add the suggester.

        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)

        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=origin_visit,
        )

        self.assertIsNotNone(suggestion)
        self.assertTrue(suggestion.offers_merge)
        self.assertEqual(suggestion.existing_visit_id, existing_visit.pk)


# ---------------------------------------------------------------------------
# accept_visit_suggestion
# ---------------------------------------------------------------------------

class AcceptVisitSuggestionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.suggester = baker.make("auth.User").profile
        self.recipient = baker.make("auth.User").profile
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0")
        self.visited_at = timezone.make_aware(datetime.datetime(2026, 6, 1, 14, 0))

    def _make_suggestion(self, **overrides) -> VisitSuggestion:
        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)
        defaults = dict(
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            visited_at=self.visited_at,
            suggested_by=self.suggester,
            suggested_to=self.recipient,
            origin_visit=origin_visit,
        )
        defaults.update(overrides)
        return baker.make(VisitSuggestion, **defaults)

    def test_creates_new_pin_when_recipient_has_none(self) -> None:
        suggestion = self._make_suggestion()
        self.assertFalse(Pin.objects.filter(profile=self.recipient).exists())

        visit = accept_visit_suggestion(suggestion, self.recipient)

        self.assertTrue(Pin.objects.filter(profile=self.recipient, location=self.location).exists())
        self.assertEqual(visit.pin.profile, self.recipient)

    def test_reuses_existing_pin(self) -> None:
        existing_pin = baker.make(Pin, profile=self.recipient, location=self.location)
        suggestion = self._make_suggestion()

        visit = accept_visit_suggestion(suggestion, self.recipient)

        self.assertEqual(visit.pin, existing_pin)
        self.assertEqual(Pin.objects.filter(profile=self.recipient).count(), 1)

    def test_source_is_user_for_manual_dialog_flow(self) -> None:
        suggestion = self._make_suggestion()
        visit = accept_visit_suggestion(suggestion, self.recipient)
        self.assertEqual(visit.source, VisitSource.USER)

    def test_source_is_trip_for_trip_flow(self) -> None:
        trip = Trip.objects.create(name="Test Trip", creator=self.suggester)
        activity = TripActivity.objects.create(trip=trip, added_by=self.suggester, location=self.location, title="Explore")
        suggestion = self._make_suggestion(origin_visit=None, trip_activity=activity)

        visit = accept_visit_suggestion(suggestion, self.recipient)

        self.assertEqual(visit.source, VisitSource.TRIP)

    def test_only_mutual_connections_are_linked_as_participants(self) -> None:
        stranger = baker.make("auth.User").profile
        _make_accepted_friendship(self.recipient, self.suggester)
        # stranger is NOT connected to recipient.
        suggestion = self._make_suggestion()
        suggestion.candidate_profiles.set([stranger])

        visit = accept_visit_suggestion(suggestion, self.recipient)

        participant_ids = set(visit.participants.values_list("pk", flat=True))
        self.assertIn(self.suggester.pk, participant_ids)
        self.assertNotIn(stranger.pk, participant_ids)

    def test_suggested_by_excluded_when_not_a_mutual_connection(self) -> None:
        """A trip co-member (suggested_by) who isn't actually a friend must not be linked."""
        suggestion = self._make_suggestion()  # no friendship created between suggester and recipient

        visit = accept_visit_suggestion(suggestion, self.recipient)

        self.assertEqual(visit.participants.count(), 0)

    def test_marks_suggestion_accepted(self) -> None:
        suggestion = self._make_suggestion()
        accept_visit_suggestion(suggestion, self.recipient)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, VisitSuggestionStatus.ACCEPTED)


# ---------------------------------------------------------------------------
# merge_visit_suggestion
# ---------------------------------------------------------------------------

class MergeVisitSuggestionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.suggester = baker.make("auth.User").profile
        self.recipient = baker.make("auth.User").profile
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0")
        self.visited_at = timezone.make_aware(datetime.datetime(2026, 6, 1, 14, 0))
        _make_accepted_friendship(self.recipient, self.suggester)

        self.recipient_pin = baker.make(Pin, profile=self.recipient, location=self.location)
        self.existing_visit = baker.make(PinVisit, pin=self.recipient_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)

        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)
        self.suggestion = baker.make(
            VisitSuggestion,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            visited_at=self.visited_at,
            suggested_by=self.suggester,
            suggested_to=self.recipient,
            origin_visit=origin_visit,
            existing_visit=self.existing_visit,
        )

    def test_adds_new_participant_to_existing_visit(self) -> None:
        merge_visit_suggestion(self.suggestion, self.recipient)
        self.existing_visit.refresh_from_db()
        self.assertIn(self.suggester, self.existing_visit.participants.all())

    def test_does_not_create_a_new_pinvisit(self) -> None:
        count_before = PinVisit.objects.filter(pin=self.recipient_pin).count()
        merge_visit_suggestion(self.suggestion, self.recipient)
        self.assertEqual(PinVisit.objects.filter(pin=self.recipient_pin).count(), count_before)

    def test_does_not_duplicate_already_present_participant(self) -> None:
        self.existing_visit.participants.add(self.suggester)
        merge_visit_suggestion(self.suggestion, self.recipient)
        self.assertEqual(list(self.existing_visit.participants.values_list("pk", flat=True)), [self.suggester.pk])

    def test_marks_suggestion_accepted(self) -> None:
        merge_visit_suggestion(self.suggestion, self.recipient)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, VisitSuggestionStatus.ACCEPTED)


# ---------------------------------------------------------------------------
# Trip activity completion: completer auto-logs, other rsvp=yes members get suggestions
# ---------------------------------------------------------------------------

class TripActivityCompletionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.completer = baker.make("auth.User").profile
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0")
        self.trip = Trip.objects.create(name="Test Trip", creator=self.completer)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.completer, defaults={"rsvp": "yes"})
        self.activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.completer,
            location=self.location,
            title="Explore Site",
            status=TripActivity.STATUS_PROPOSED,
            scheduled_at=timezone.make_aware(datetime.datetime(2026, 6, 1, 12, 0)),
        )
        self.client = Client()
        self.client.force_login(self.completer.user)

    def _complete_url(self) -> str:
        return reverse("trips.activity.complete", kwargs={"trip_uuid": str(self.trip.uuid), "activity_id": self.activity.id})

    def test_completer_gets_an_immediate_visit(self) -> None:
        self.client.post(self._complete_url(), data={"completed_date": "2026-06-01"})
        self.assertTrue(PinVisit.objects.filter(pin__profile=self.completer, source=VisitSource.TRIP).exists())

    def test_other_rsvp_yes_member_gets_a_suggestion_not_a_visit(self) -> None:
        yes_member = baker.make("auth.User").profile
        TripMembership.objects.create(trip=self.trip, profile=yes_member, rsvp=TripMembership.RSVP_YES)

        self.client.post(self._complete_url(), data={"completed_date": "2026-06-01"})

        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=yes_member, trip_activity=self.activity).exists())
        self.assertFalse(PinVisit.objects.filter(pin__profile=yes_member).exists())

    def test_rsvp_no_member_gets_nothing(self) -> None:
        no_member = baker.make("auth.User").profile
        TripMembership.objects.create(trip=self.trip, profile=no_member, rsvp=TripMembership.RSVP_NO)

        self.client.post(self._complete_url(), data={"completed_date": "2026-06-01"})

        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=no_member).exists())
        self.assertFalse(PinVisit.objects.filter(pin__profile=no_member).exists())

    def test_rsvp_maybe_member_gets_nothing(self) -> None:
        maybe_member = baker.make("auth.User").profile
        TripMembership.objects.create(trip=self.trip, profile=maybe_member, rsvp=TripMembership.RSVP_MAYBE)

        self.client.post(self._complete_url(), data={"completed_date": "2026-06-01"})

        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=maybe_member).exists())

    def test_completion_succeeds_even_without_resolvable_coordinates(self) -> None:
        self.activity.location = None
        self.activity.save(update_fields=["location"])

        resp = self.client.post(self._complete_url(), data={"completed_date": "2026-06-01"})

        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, TripActivity.STATUS_COMPLETED)
        self.assertEqual(resp.status_code, 200)
