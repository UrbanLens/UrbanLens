"""Regression test for TripQuerySet.for_list_page's member_count annotation.

`.filter(profiles=profile)` and the `Count("memberships")` annotation both
join through Trip -> TripMembership; Django reused that single join, so the
membership-count annotation inherited the filter's `profile_id = viewer`
clause and always came out as 1 regardless of how many actual members a trip
had. Fixed by filtering via a pk subquery instead, so the annotation gets its
own unfiltered join.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripMembership

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class TripListMemberCountTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_member_count_reflects_every_member_not_just_the_viewer(self) -> None:
        trip = Trip.objects.create(name="Group Trip", creator=self.profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, is_organizer=True)
        other_profile = baker.make(User).profile
        TripMembership.objects.create(trip=trip, profile=other_profile)

        annotated = Trip.objects.for_list_page(self.profile).get(pk=trip.pk)

        self.assertEqual(annotated.member_count, 2)

    def test_member_count_is_one_for_a_solo_trip(self) -> None:
        trip = Trip.objects.create(name="Solo Trip", creator=self.profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, is_organizer=True)

        annotated = Trip.objects.for_list_page(self.profile).get(pk=trip.pk)

        self.assertEqual(annotated.member_count, 1)

    def test_member_count_unaffected_by_other_members_own_trips(self) -> None:
        """A second, unrelated trip the viewer isn't part of must not affect this one's count."""
        trip = Trip.objects.create(name="Group Trip", creator=self.profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, is_organizer=True)
        other_profile = baker.make(User).profile
        TripMembership.objects.create(trip=trip, profile=other_profile)

        other_trip = Trip.objects.create(name="Someone Else's Trip", creator=other_profile)
        TripMembership.objects.create(trip=other_trip, profile=other_profile, is_organizer=True)

        annotated = Trip.objects.for_list_page(self.profile).get(pk=trip.pk)

        self.assertEqual(annotated.member_count, 2)
