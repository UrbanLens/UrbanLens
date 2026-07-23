"""Tests for trip-activity share tracking (services.trip_share_tracking).

Putting a place on a trip's itinerary reveals it to every joined member, and
that must count in the sharer's reshare chain like any other pin share -
these tests cover both directions (new activity → existing members, new
member → existing activities) and the dedup rules (the adder themselves,
members who already have the place pinned, members already exposed, and
hidden-location activities never produce shares).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import LocationExposure, PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.trip_share_tracking import record_trip_activity_shares, record_trip_shares_for_member


class _TripShareTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.users = {name: baker.make(User, username=name) for name in ("owner", "member", "invited")}
        self.profiles = {name: user.profile for name, user in self.users.items()}
        self.location = baker.make(Location, latitude="42.100000", longitude="-73.900000")
        self.owner_pin = Pin.objects.create(profile=self.profiles["owner"], location=self.location)
        self.trip = Trip.objects.create(name="Mill run", creator=self.profiles["owner"])
        TripMembership.objects.create(trip=self.trip, profile=self.profiles["owner"], status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=self.trip, profile=self.profiles["member"], status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=self.trip, profile=self.profiles["invited"], status=TripMembership.STATUS_INVITED)

    def _activity(self, **kwargs) -> TripActivity:
        defaults = {"trip": self.trip, "pin": self.owner_pin, "location": self.location, "added_by": self.profiles["owner"]}
        defaults.update(kwargs)
        return TripActivity.objects.create(**defaults)


class RecordTripActivityShareTests(_TripShareTestCase):
    """A new activity records detected shares to joined members only."""

    def test_activity_shares_to_joined_members(self):
        shares = record_trip_activity_shares(self._activity())

        self.assertEqual(len(shares), 1)
        share = shares[0]
        self.assertEqual(share.origin, PinShareOrigin.TRIP_ACTIVITY)
        self.assertEqual(share.status, PinShareStatus.DETECTED)
        self.assertEqual(share.from_profile_id, self.profiles["owner"].pk)
        self.assertEqual(share.to_profile_id, self.profiles["member"].pk)
        self.assertEqual(share.location_id, self.location.pk)
        self.assertTrue(LocationExposure.objects.filter(profile=self.profiles["member"], share=share).exists())
        # The invited-but-not-joined member can't see the itinerary yet.
        self.assertFalse(PinShare.objects.filter(to_profile=self.profiles["invited"]).exists())

    def test_member_with_own_pin_gets_no_share(self):
        Pin.objects.create(profile=self.profiles["member"], location=self.location)
        shares = record_trip_activity_shares(self._activity())
        self.assertEqual(shares, [])

    def test_hidden_location_activity_shares_nothing(self):
        shares = record_trip_activity_shares(self._activity(location_hidden=True))
        self.assertEqual(shares, [])

    def test_second_activity_at_same_place_does_not_double_count(self):
        record_trip_activity_shares(self._activity())
        shares = record_trip_activity_shares(self._activity(pin=None))
        self.assertEqual(shares, [])
        self.assertEqual(PinShare.objects.filter(to_profile=self.profiles["member"]).count(), 1)


class RecordTripMemberJoinTests(_TripShareTestCase):
    """Joining a trip reveals the existing itinerary to the new member."""

    def test_join_records_shares_for_existing_activities(self):
        activity = self._activity()
        record_trip_activity_shares(activity)

        TripMembership.objects.filter(trip=self.trip, profile=self.profiles["invited"]).update(status=TripMembership.STATUS_JOINED)
        shares = record_trip_shares_for_member(self.trip, self.profiles["invited"])

        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].to_profile_id, self.profiles["invited"].pk)
        self.assertEqual(shares[0].origin, PinShareOrigin.TRIP_ACTIVITY)
        self.assertTrue(LocationExposure.objects.filter(profile=self.profiles["invited"], share=shares[0]).exists())

    def test_join_skips_own_activities(self):
        self._activity(added_by=self.profiles["invited"])
        shares = record_trip_shares_for_member(self.trip, self.profiles["invited"])
        self.assertEqual(shares, [])
