"""Tests for AI trip suggestions (UL-60): the privacy gates, context assembly, and endpoints."""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripActivityVote, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.trip_ai_suggestions import (
    ExistingActivity,
    ScheduleSuggestion,
    TripAiContext,
    TripSuggestions,
    _build_candidates,
    _common_location_ids,
    _joined_profiles,
    _resolve_pin_suggestions,
    _resolve_schedule,
    build_trip_context,
    generate_trip_suggestions,
    get_trip_suggestions,
)


class _StubGateway:
    """A minimal LLMGateway stand-in that returns a fixed answer."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        return self.answer


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _joined_trip(*members: Profile, creator: Profile | None = None) -> Trip:
    creator = creator or members[0]
    trip = baker.make(Trip, name="Suggestion Test Trip", creator=creator)
    for member in {creator, *members}:
        baker.make(TripMembership, trip=trip, profile=member, status=TripMembership.STATUS_JOINED, rsvp="yes")
    return trip


class JoinedProfilesTests(TestCase):
    """The creator always counts as joined, even without an explicit row."""

    def setUp(self) -> None:
        baker.make("auth.User")

    def test_creator_without_membership_row_still_counts(self) -> None:
        creator = _profile()
        trip = baker.make(Trip, creator=creator)
        self.assertEqual({p.id for p in _joined_profiles(trip)}, {creator.id})

    def test_invited_but_not_joined_members_are_excluded(self) -> None:
        creator = _profile()
        invited = _profile()
        trip = baker.make(Trip, creator=creator)
        baker.make(TripMembership, trip=trip, profile=creator, status=TripMembership.STATUS_JOINED)
        baker.make(TripMembership, trip=trip, profile=invited, status=TripMembership.STATUS_INVITED)
        self.assertEqual({p.id for p in _joined_profiles(trip)}, {creator.id})


class CommonLocationIntersectionTests(TestCase):
    """The anti-leak candidate gate: a location must belong to every given profile."""

    def setUp(self) -> None:
        baker.make("auth.User")

    def test_only_locations_every_profile_has_are_common(self) -> None:
        alice, bob = _profile(), _profile()
        shared = baker.make(Location, latitude="41.000000", longitude="-72.000000")
        alice_only = baker.make(Location, latitude="41.100000", longitude="-72.100000")
        baker.make(Pin, profile=alice, location=shared)
        baker.make(Pin, profile=bob, location=shared)
        baker.make(Pin, profile=alice, location=alice_only)
        self.assertEqual(_common_location_ids([alice, bob]), {shared.id})

    def test_no_profiles_means_no_candidates(self) -> None:
        self.assertEqual(_common_location_ids([]), set())

    def test_solo_trip_treats_all_own_pins_as_common(self) -> None:
        alice = _profile()
        loc = baker.make(Location, latitude="41.200000", longitude="-72.200000")
        baker.make(Pin, profile=alice, location=loc)
        self.assertEqual(_common_location_ids([alice]), {loc.id})

    def test_child_pins_never_count(self) -> None:
        alice, bob = _profile(), _profile()
        loc = baker.make(Location, latitude="41.300000", longitude="-72.300000")
        parent = baker.make(Pin, profile=alice, location=loc)
        baker.make(Pin, profile=bob, location=loc)
        baker.make(Pin, profile=alice, location=baker.make(Location, latitude="41.310000", longitude="-72.310000"), parent_pin=parent)
        # alice's detail pin doesn't create a second root-pin location, so the
        # only common location remains the one both actually pinned.
        self.assertEqual(_common_location_ids([alice, bob]), {loc.id})


class BuildCandidatesPrivacyTests(TestCase):
    """The two independent privacy gates on candidate construction."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        self.bob = _profile()
        self.shared_loc = baker.make(Location, latitude="40.000000", longitude="-73.000000")
        self.alice_pin = baker.make(Pin, profile=self.alice, location=self.shared_loc, priority=4, danger=2)
        self.bob_pin = baker.make(Pin, profile=self.bob, location=self.shared_loc, priority=2, vulnerability=3)

    def test_location_only_one_member_has_is_never_a_candidate(self) -> None:
        private_loc = baker.make(Location, latitude="40.500000", longitude="-73.500000")
        baker.make(Pin, profile=self.alice, location=private_loc)
        candidates = _build_candidates([self.alice, self.bob], self.alice, set())
        self.assertEqual({c.location_id for c in candidates}, {self.shared_loc.id})

    def test_already_added_locations_are_excluded(self) -> None:
        candidates = _build_candidates([self.alice, self.bob], self.alice, {self.shared_loc.id})
        self.assertEqual(candidates, [])

    def test_sharing_disabled_member_contributes_no_personal_signal(self) -> None:
        Profile.objects.filter(pk=self.bob.pk).update(external_apis_enabled=False)
        self.bob.refresh_from_db()
        candidates = _build_candidates([self.alice, self.bob], self.alice, set())
        self.assertEqual(len(candidates), 1)
        labels = {s.member_label for s in candidates[0].signals}
        # Only one sharing-enabled member (alice) contributed a signal -
        # bob's pin exists (required for candidacy) but his ratings are gone.
        self.assertEqual(len(candidates[0].signals), 1)
        self.assertEqual(labels, {"Member 1"})

    def test_sharing_disabled_member_still_required_for_candidacy(self) -> None:
        # Even with sharing off, bob having (or not having) the pin still
        # gates whether the location is common - opting out of sharing data
        # never expands what other members can be shown.
        Profile.objects.filter(pk=self.bob.pk).update(external_apis_enabled=False)
        self.bob.refresh_from_db()
        self.bob_pin.delete()
        candidates = _build_candidates([self.alice, self.bob], self.alice, set())
        self.assertEqual(candidates, [])

    def test_member_labels_are_anonymous_and_dense(self) -> None:
        candidates = _build_candidates([self.alice, self.bob], self.alice, set())
        labels = {s.member_label for s in candidates[0].signals}
        self.assertEqual(labels, {"Member 1", "Member 2"})
        for signal in candidates[0].signals:
            self.assertNotIn(self.alice.user.username, signal.member_label)
            self.assertNotIn(self.bob.user.username, signal.member_label)

    def test_visited_status_reflects_pin_visit_records(self) -> None:
        baker.make(PinVisit, pin=self.alice_pin)
        candidates = _build_candidates([self.alice, self.bob], self.alice, set())
        visited_flags = {s.visited for s in candidates[0].signals}
        self.assertEqual(visited_flags, {True, False})

    def test_zero_ratings_are_treated_as_unrated_not_zero(self) -> None:
        # Pin defaults priority/vulnerability/danger to 0 ("unrated"), not a
        # real rating of zero - must never be sent to the model as "0/5".
        # alice_pin has no vulnerability set (default 0); bob_pin has no danger set.
        candidates = _build_candidates([self.alice, self.bob], self.alice, set())
        alice_signal = next(s for s in candidates[0].signals if s.priority == 4)
        self.assertIsNone(alice_signal.vulnerability)
        bob_signal = next(s for s in candidates[0].signals if s.priority == 2)
        self.assertIsNone(bob_signal.danger)

    def test_add_pin_slug_is_always_the_requesters_own(self) -> None:
        candidates = _build_candidates([self.alice, self.bob], self.bob, set())
        self.assertEqual(candidates[0].add_pin_slug, self.bob_pin.slug)


class BuildTripContextTests(TestCase):
    """Context assembly wires dates/participants/activities/legs together."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        self.bob = _profile()
        self.trip = _joined_trip(self.alice, self.bob)

    def test_participant_count_matches_joined_members(self) -> None:
        context = build_trip_context(self.trip, self.alice)
        self.assertEqual(context.participant_count, 2)

    def test_hidden_activity_location_never_reaches_the_context(self) -> None:
        from urbanlens.dashboard.models.profile.model import VisibilityChoice

        loc = baker.make(Location, latitude="39.000000", longitude="-71.000000")
        Profile.objects.filter(pk=self.bob.pk).update(trip_pin_location_visibility=VisibilityChoice.NO_ONE)
        self.bob.refresh_from_db()
        baker.make(TripActivity, trip=self.trip, location=loc, added_by=self.bob, title="Bob's secret stop")
        context = build_trip_context(self.trip, self.alice)
        self.assertEqual(context.activities, [])

    def test_visible_activity_appears_with_votes(self) -> None:
        loc = baker.make(Location, latitude="39.100000", longitude="-71.100000")
        activity = baker.make(TripActivity, trip=self.trip, location=loc, added_by=self.alice, title="Open stop")
        baker.make(TripActivityVote, activity=activity, profile=self.alice, vote="up")
        baker.make(TripActivityVote, activity=activity, profile=self.bob, vote="down")
        context = build_trip_context(self.trip, self.alice)
        self.assertEqual(len(context.activities), 1)
        self.assertEqual(context.activities[0].votes_up, 1)
        self.assertEqual(context.activities[0].votes_down, 1)

    def test_completed_activities_are_excluded(self) -> None:
        loc = baker.make(Location, latitude="39.200000", longitude="-71.200000")
        baker.make(TripActivity, trip=self.trip, location=loc, added_by=self.alice, status=TripActivity.STATUS_COMPLETED)
        context = build_trip_context(self.trip, self.alice)
        self.assertEqual(context.activities, [])


class ResolveModelOutputTests(TestCase):
    """Parsing/validating the model's JSON response never trusts it blindly."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        loc = baker.make(Location, latitude="38.000000", longitude="-70.000000")
        self.pin = baker.make(Pin, profile=self.alice, location=loc)
        candidates = _build_candidates([self.alice], self.alice, set())
        self.context = TripAiContext(trip_name="t", start_date=None, end_date=None, duration_days=None, participant_count=1, candidates=candidates)

    def test_valid_index_resolves_to_candidate(self) -> None:
        result = _resolve_pin_suggestions({"pin_suggestions": [{"index": 1, "reason": "Nobody's been."}]}, self.context)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].location_id, self.context.candidates[0].location_id)

    def test_out_of_range_index_is_dropped(self) -> None:
        result = _resolve_pin_suggestions({"pin_suggestions": [{"index": 99, "reason": "hallucinated"}]}, self.context)
        self.assertEqual(result, [])

    def test_duplicate_indices_collapse_to_one(self) -> None:
        result = _resolve_pin_suggestions({"pin_suggestions": [{"index": 1, "reason": "a"}, {"index": 1, "reason": "b"}]}, self.context)
        self.assertEqual(len(result), 1)

    def test_schedule_must_be_exact_permutation(self) -> None:
        context = TripAiContext(
            trip_name="t",
            start_date=None,
            end_date=None,
            duration_days=None,
            participant_count=1,
            activities=[
                ExistingActivity(activity_id=1, title="A", status="proposed", scheduled_at=None, votes_up=0, votes_down=0),
                ExistingActivity(activity_id=2, title="B", status="proposed", scheduled_at=None, votes_up=0, votes_down=0),
            ],
        )
        valid = _resolve_schedule({"schedule": {"order": [2, 1], "reason": "drive time"}}, context)
        self.assertIsInstance(valid, ScheduleSuggestion)
        self.assertEqual(valid.ordered_activity_ids, [2, 1])

    def test_schedule_with_missing_activity_is_rejected(self) -> None:
        context = TripAiContext(
            trip_name="t",
            start_date=None,
            end_date=None,
            duration_days=None,
            participant_count=1,
            activities=[
                ExistingActivity(activity_id=1, title="A", status="proposed", scheduled_at=None, votes_up=0, votes_down=0),
                ExistingActivity(activity_id=2, title="B", status="proposed", scheduled_at=None, votes_up=0, votes_down=0),
            ],
        )
        result = _resolve_schedule({"schedule": {"order": [1], "reason": "partial"}}, context)
        self.assertIsNone(result)

    def test_schedule_with_extra_activity_is_rejected(self) -> None:
        context = TripAiContext(
            trip_name="t",
            start_date=None,
            end_date=None,
            duration_days=None,
            participant_count=1,
            activities=[ExistingActivity(activity_id=1, title="A", status="proposed", scheduled_at=None, votes_up=0, votes_down=0)],
        )
        result = _resolve_schedule({"schedule": {"order": [1, 999], "reason": "hallucinated"}}, context)
        self.assertIsNone(result)

    def test_no_schedule_key_is_fine(self) -> None:
        self.assertIsNone(_resolve_schedule({}, self.context))


class GenerateTripSuggestionsTests(TestCase):
    """The end-to-end generation flow, AI gateway mocked."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        self.trip = _joined_trip(self.alice)

    def test_unavailable_when_gateway_is_none(self) -> None:
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=None):
            result = generate_trip_suggestions(self.trip, self.alice)
        self.assertFalse(result.generated)

    def test_site_setting_off_disables_the_feature(self) -> None:
        site = SiteSettings.get_current()
        site.ai_trip_suggestions_enabled = False
        site.save(update_fields=["ai_trip_suggestions_enabled"])
        result = generate_trip_suggestions(self.trip, self.alice)
        self.assertFalse(result.generated)

    def test_successful_generation_parses_summary(self) -> None:
        gateway = _StubGateway('{"summary": "Looks good.", "pin_suggestions": [], "schedule": null}')
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=gateway):
            result = generate_trip_suggestions(self.trip, self.alice)
        self.assertTrue(result.generated)
        self.assertEqual(result.summary, "Looks good.")

    def test_no_answer_from_gateway_is_handled(self) -> None:
        gateway = _StubGateway(None)
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=gateway):
            result = generate_trip_suggestions(self.trip, self.alice)
        self.assertTrue(result.generated)
        self.assertIn("Couldn't reach", result.summary)

    def test_unparseable_answer_is_handled(self) -> None:
        gateway = _StubGateway("not json at all")
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=gateway):
            result = generate_trip_suggestions(self.trip, self.alice)
        self.assertTrue(result.generated)
        self.assertIn("couldn't be understood", result.summary)


class GetTripSuggestionsCacheTests(TestCase):
    """Caching and the refresh cooldown."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        self.trip = _joined_trip(self.alice)
        from django.core.cache import cache

        cache.clear()

    def test_second_call_is_served_from_cache(self) -> None:
        gateway = _StubGateway('{"summary": "first"}')
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=gateway) as mocked:
            get_trip_suggestions(self.trip, self.alice)
            get_trip_suggestions(self.trip, self.alice)
        self.assertEqual(mocked.call_count, 1)

    def test_force_refresh_bypasses_cache_once(self) -> None:
        gateway = _StubGateway('{"summary": "fresh"}')
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=gateway) as mocked:
            get_trip_suggestions(self.trip, self.alice)
            get_trip_suggestions(self.trip, self.alice, force_refresh=True)
        self.assertEqual(mocked.call_count, 2)

    def test_rapid_force_refresh_is_cooldown_limited(self) -> None:
        gateway = _StubGateway('{"summary": "x"}')
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_gateway", return_value=gateway) as mocked:
            get_trip_suggestions(self.trip, self.alice, force_refresh=True)
            get_trip_suggestions(self.trip, self.alice, force_refresh=True)
        self.assertEqual(mocked.call_count, 1)


class TripAiSuggestionsViewTests(TestCase):
    """The endpoint: membership gating, rendering, and the apply-order action."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        self.bob = _profile()
        self.trip = _joined_trip(self.alice, self.bob)
        self.client.force_login(self.alice.user)

    def test_non_member_is_rejected(self) -> None:
        outsider = _profile()
        self.client.force_login(outsider.user)
        response = self.client.get(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        self.assertEqual(response.status_code, 403)

    def test_invited_but_not_joined_is_rejected(self) -> None:
        invited = _profile()
        baker.make(TripMembership, trip=self.trip, profile=invited, status=TripMembership.STATUS_INVITED)
        self.client.force_login(invited.user)
        response = self.client.get(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        self.assertEqual(response.status_code, 403)

    def test_joined_member_sees_disabled_state_when_ai_off(self) -> None:
        Profile.objects.filter(pk=self.alice.pk).update(ai_enabled=False)
        response = self.client.get(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "turned off")

    def test_get_renders_generated_suggestions(self) -> None:
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_trip_suggestions") as mocked:
            mocked.return_value = TripSuggestions(summary="A great plan.", pin_suggestions=[], schedule=None)
            response = self.client.get(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A great plan.")
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], self.trip)
        self.assertEqual(args[1], self.alice)
        self.assertEqual(kwargs, {"force_refresh": False})

    def test_post_forces_refresh(self) -> None:
        with patch("urbanlens.dashboard.services.trip_ai_suggestions.get_trip_suggestions") as mocked:
            mocked.return_value = TripSuggestions(summary="Refreshed.", pin_suggestions=[], schedule=None)
            self.client.post(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        _, kwargs = mocked.call_args
        self.assertEqual(kwargs, {"force_refresh": True})


class ApplySuggestedOrderViewTests(TestCase):
    """Reordering only ever accepts an exact permutation of current activities."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.alice = _profile()
        self.trip = _joined_trip(self.alice)
        self.client.force_login(self.alice.user)
        loc_a = baker.make(Location, latitude="37.000000", longitude="-69.000000")
        loc_b = baker.make(Location, latitude="37.100000", longitude="-69.100000")
        self.act_a = baker.make(TripActivity, trip=self.trip, location=loc_a, added_by=self.alice, order=0)
        self.act_b = baker.make(TripActivity, trip=self.trip, location=loc_b, added_by=self.alice, order=1)
        self.url = reverse("trips.activity.apply_order", args=[self.trip.slug])

    def test_valid_permutation_applies(self) -> None:
        response = self.client.post(self.url, data=f'{{"order": [{self.act_b.id}, {self.act_a.id}]}}', content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.act_a.refresh_from_db()
        self.act_b.refresh_from_db()
        self.assertEqual(self.act_b.order, 0)
        self.assertEqual(self.act_a.order, 1)

    def test_partial_order_is_rejected(self) -> None:
        response = self.client.post(self.url, data=f'{{"order": [{self.act_a.id}]}}', content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_order_with_foreign_activity_is_rejected(self) -> None:
        other_trip = _joined_trip(self.alice)
        other_loc = baker.make(Location, latitude="37.200000", longitude="-69.200000")
        foreign = baker.make(TripActivity, trip=other_trip, location=other_loc, added_by=self.alice)
        response = self.client.post(self.url, data=f'{{"order": [{self.act_a.id}, {foreign.id}]}}', content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.act_a.refresh_from_db()
        self.assertEqual(self.act_a.order, 0)

    def test_no_permission_is_rejected(self) -> None:
        Trip.objects.filter(pk=self.trip.pk).update(allow_edit_activities="none")
        bob = _profile()
        baker.make(TripMembership, trip=self.trip, profile=bob, status=TripMembership.STATUS_JOINED)
        self.client.force_login(bob.user)
        response = self.client.post(self.url, data=f'{{"order": [{self.act_a.id}, {self.act_b.id}]}}', content_type="application/json")
        self.assertEqual(response.status_code, 403)
