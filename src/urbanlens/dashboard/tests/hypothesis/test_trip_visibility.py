"""Tests for _apply_trip_visibility_filter - the core privacy logic for trip activities.

This function hides activities from viewers based on the adder's
trip_pin_location_visibility setting:
  - NO_ONE    → always hidden
  - FRIENDS   → visible only if viewer and adder are accepted friends
  - COMMON_PIN → visible only if viewer also has the same Location pinned
  - COMMON_FRIEND → visible only if viewer and adder share a mutual friend
  - ANYONE    → always visible (handled before calling this function)

All tests are DB-backed since the function queries Pin, Friendship, and related models.
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.trip_visibility import apply_trip_visibility_filter as _apply_trip_visibility_filter


def _make_user_with_profile(username=None):
    user = baker.make("auth.User", **({"username": username} if username else {}))
    return user, user.profile


def _accept_friendship(a: Profile, b: Profile) -> Friendship:
    return Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
    )


def _make_activity_for(
    trip: Trip,
    adder: Profile,
    location: Location,
    visibility: str,
    location_hidden: bool = False,
) -> TripActivity:
    adder.trip_pin_location_visibility = visibility
    adder.save(update_fields=["trip_pin_location_visibility"])
    return TripActivity.objects.create(
        trip=trip,
        added_by=adder,
        location=location,
        title="Test Activity",
        location_hidden=location_hidden,
    )


class NoOneVisibilityTests(TestCase):
    """NO_ONE → activity always hidden regardless of friendship or pins."""

    def setUp(self):
        super().setUp()
        _, self.viewer = _make_user_with_profile()
        _, self.adder = _make_user_with_profile()
        self.location = baker.make(Location, official_name="Secret Spot")
        self.trip = Trip.objects.create(name="Test Trip", creator=self.adder)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)

    def test_no_one_activity_always_hidden(self):
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.NO_ONE
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)

    def test_no_one_hidden_even_for_friend(self):
        _accept_friendship(self.viewer, self.adder)
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.NO_ONE
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)

    def test_no_one_hidden_even_with_common_pin(self):
        Pin.objects.create(
            profile=self.viewer,
            location=self.location,
        )
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.NO_ONE
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)


class FriendsVisibilityTests(TestCase):
    """FRIENDS → visible only to accepted friends of the adder."""

    def setUp(self):
        super().setUp()
        _, self.viewer = _make_user_with_profile()
        _, self.adder = _make_user_with_profile()
        self.location = baker.make(Location, official_name="Abandoned Mill")
        self.trip = Trip.objects.create(name="Friends Trip", creator=self.adder)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)

    def test_non_friend_cannot_see_location(self):
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.FRIENDS
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)

    def test_friend_can_see_location(self):
        _accept_friendship(self.viewer, self.adder)
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.FRIENDS
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_reversed_friendship_still_visible(self):
        # Friend stored as (adder→viewer) rather than (viewer→adder)
        _accept_friendship(self.adder, self.viewer)
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.FRIENDS
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_pending_friendship_does_not_grant_access(self):
        Friendship.objects.create(
            from_profile=self.viewer,
            to_profile=self.adder,
            status=FriendshipStatus.PENDING,
        )
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.FRIENDS
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)


class CommonPinVisibilityTests(TestCase):
    """COMMON_PIN → visible only if viewer also has the same location pinned."""

    def setUp(self):
        super().setUp()
        _, self.viewer = _make_user_with_profile()
        _, self.adder = _make_user_with_profile()
        self.location = baker.make(Location, official_name="The Factory", latitude=10.0, longitude=20.0)
        self.trip = Trip.objects.create(name="Pin Trip", creator=self.adder)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)

    def test_viewer_without_pin_cannot_see(self):
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.COMMON_PIN
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)

    def test_viewer_with_same_pin_can_see(self):
        Pin.objects.create(
            profile=self.viewer,
            location=self.location,
        )
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.COMMON_PIN
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_viewer_with_different_pin_cannot_see(self):
        other_location = baker.make(Location, official_name="Other Spot")
        Pin.objects.create(
            profile=self.viewer,
            location=other_location,
        )
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.COMMON_PIN
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)


class CommonFriendVisibilityTests(TestCase):
    """COMMON_FRIEND → visible only if viewer and adder share a mutual friend."""

    def setUp(self):
        super().setUp()
        _, self.viewer = _make_user_with_profile()
        _, self.adder = _make_user_with_profile()
        _, self.mutual_friend = _make_user_with_profile()
        self.location = baker.make(Location, official_name="Shared Spot")
        self.trip = Trip.objects.create(name="CFriend Trip", creator=self.adder)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)

    def test_no_shared_friend_cannot_see(self):
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.COMMON_FRIEND
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)

    def test_shared_mutual_friend_grants_access(self):
        _accept_friendship(self.viewer, self.mutual_friend)
        _accept_friendship(self.adder, self.mutual_friend)
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.COMMON_FRIEND
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_unrelated_friend_does_not_grant_access(self):
        _, unrelated = _make_user_with_profile()
        _accept_friendship(self.viewer, unrelated)
        act = _make_activity_for(
            self.trip, self.adder, self.location, VisibilityChoice.COMMON_FRIEND
        )
        hidden = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)


class MultipleActivitiesVisibilityTests(TestCase):
    """Mixed visibility - only NO_ONE and FRIENDS activities hidden for a non-friend."""

    def setUp(self):
        super().setUp()
        _, self.viewer = _make_user_with_profile()
        _, self.adder_no_one = _make_user_with_profile()
        _, self.adder_friends = _make_user_with_profile()
        _, self.adder_common_pin = _make_user_with_profile()
        self.location = baker.make(Location, official_name="Multi Spot", latitude=5.0, longitude=10.0)
        self.trip = Trip.objects.create(name="Multi Trip", creator=self.adder_no_one)
        for p in (self.viewer, self.adder_friends, self.adder_common_pin):
            TripMembership.objects.create(trip=self.trip, profile=p)

        # Viewer has the pin for common_pin test
        Pin.objects.create(profile=self.viewer, location=self.location)

    def test_no_one_hidden_friends_hidden_common_pin_visible(self):
        act_no_one = _make_activity_for(self.trip, self.adder_no_one, self.location, VisibilityChoice.NO_ONE)
        act_friends = _make_activity_for(self.trip, self.adder_friends, self.location, VisibilityChoice.FRIENDS)
        act_pin = _make_activity_for(self.trip, self.adder_common_pin, self.location, VisibilityChoice.COMMON_PIN)

        sensitive = [act_no_one, act_friends, act_pin]
        hidden = set()
        _apply_trip_visibility_filter(sensitive, self.viewer, hidden)

        self.assertIn(act_no_one.id, hidden)
        self.assertIn(act_friends.id, hidden)
        self.assertNotIn(act_pin.id, hidden)
