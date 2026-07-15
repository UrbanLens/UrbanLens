"""Tests for the six admin-configurable site limits added to SiteSettings.

Covers:
- max_friends_per_user - Friendship.accept() refuses once either side is at the cap.
- max_pins_per_list - PinListAddPinsView truncates a batch add to fit.
- max_upcoming_trips_per_user - TripCreateView refuses a new trip past the cap.
- max_trip_activities - TripActivitiesView refuses a new activity past the cap.
- max_safety_checkin_contacts - validate_notifiable_contacts rejects past the cap.

Every setting is 0 = unlimited; each test suite verifies both the enforced
case and that 0 disables enforcement entirely.
"""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.safety import validate_notifiable_contacts

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class MaxFriendsPerUserTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile_a = baker.make(User).profile
        self.profile_b = baker.make(User).profile

    def _requested(self, from_profile: Profile, to_profile: Profile) -> Friendship:
        return Friendship.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=FriendshipStatus.REQUESTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )

    def test_accept_blocked_once_requester_is_at_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_friends_per_user = 1
        settings.save()

        # profile_a already has one accepted friend.
        existing_friend = baker.make(User).profile
        Friendship.objects.create(
            from_profile=self.profile_a,
            to_profile=existing_friend,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )

        friendship = self._requested(self.profile_b, self.profile_a)
        self.assertFalse(friendship.accept())
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.REQUESTED)

    def test_accept_allowed_under_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_friends_per_user = 5
        settings.save()

        friendship = self._requested(self.profile_a, self.profile_b)
        self.assertTrue(friendship.accept())

    def test_zero_means_unlimited(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_friends_per_user = 0
        settings.save()

        for _ in range(3):
            other = baker.make(User).profile
            Friendship.objects.create(
                from_profile=self.profile_a,
                to_profile=other,
                status=FriendshipStatus.ACCEPTED,
                relationship_type=FriendshipType.FRIEND,
                permissions=Permission.VIEW_PROFILE,
            )

        friendship = self._requested(self.profile_b, self.profile_a)
        self.assertTrue(friendship.accept())


class MaxPinsPerListTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.pin_list = PinList.objects.create(profile=self.profile, name="Test List")

    def _url(self) -> str:
        return reverse("lists.items.add", kwargs={"list_slug": self.pin_list.slug})

    def test_add_is_truncated_to_fit_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_pins_per_list = 2
        settings.save()

        pins = [baker.make(Pin, profile=self.profile) for _ in range(3)]
        resp = self.client.post(self._url(), data={"pin_ids": [pin.pk for pin in pins]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.pin_list.items.count(), 2)

    def test_add_blocked_entirely_once_already_at_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_pins_per_list = 1
        settings.save()

        existing = baker.make(Pin, profile=self.profile)
        PinListItem.objects.create(pin_list=self.pin_list, pin=existing, order=0)

        new_pin = baker.make(Pin, profile=self.profile)
        resp = self.client.post(self._url(), data={"pin_ids": [new_pin.pk]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.pin_list.items.count(), 1)

    def test_zero_means_unlimited(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_pins_per_list = 0
        settings.save()

        pins = [baker.make(Pin, profile=self.profile) for _ in range(5)]
        resp = self.client.post(self._url(), data={"pin_ids": [pin.pk for pin in pins]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.pin_list.items.count(), 5)


class MapSidebarAddToListButtonTests(TestCase):
    """The map's pin-list sidebar hides "Add these pins to a list" once the
    number of matching pins would exceed the site's max_pins_per_list cap."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self) -> str:
        return reverse("map.pins.list")

    def test_button_hidden_when_pin_count_meets_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_pins_per_list = 2
        settings.save()
        for _ in range(2):
            baker.make(Pin, profile=self.profile)

        resp = self.client.get(self._url())
        self.assertNotIn("Add these pins to a list", resp.content.decode())

    def test_button_shown_when_pin_count_under_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_pins_per_list = 5
        settings.save()
        baker.make(Pin, profile=self.profile)

        resp = self.client.get(self._url())
        self.assertIn("Add these pins to a list", resp.content.decode())

    def test_button_always_shown_when_unlimited(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_pins_per_list = 0
        settings.save()
        baker.make(Pin, profile=self.profile)

        resp = self.client.get(self._url())
        self.assertIn("Add these pins to a list", resp.content.decode())


class MaxUpcomingTripsPerUserTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def _make_upcoming_trip(self) -> Trip:
        trip = Trip.objects.create(name="Existing Trip", creator=self.profile, start_date=datetime.date.today() + datetime.timedelta(days=3))
        TripMembership.objects.get_or_create(trip=trip, profile=self.profile, defaults={"rsvp": "yes"})
        return trip

    def test_create_blocked_once_at_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_upcoming_trips_per_user = 1
        settings.save()
        self._make_upcoming_trip()

        resp = self.client.post(reverse("trips.create"), data=json.dumps({"name": "New Trip"}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Trip.objects.filter(name="New Trip").exists())

    def test_create_allowed_under_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_upcoming_trips_per_user = 5
        settings.save()
        self._make_upcoming_trip()

        resp = self.client.post(reverse("trips.create"), data=json.dumps({"name": "New Trip"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Trip.objects.filter(name="New Trip").exists())

    def test_zero_means_unlimited(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_upcoming_trips_per_user = 0
        settings.save()
        for _ in range(3):
            self._make_upcoming_trip()

        resp = self.client.post(reverse("trips.create"), data=json.dumps({"name": "New Trip"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)


class MaxTripActivitiesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.trip = Trip.objects.create(name="Test Trip", creator=self.profile, allow_add_activities=Trip.PERM_EVERYONE)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.profile, defaults={"rsvp": "yes"})

    def _url(self) -> str:
        return reverse("trips.activities", kwargs={"trip_slug": self.trip.slug})

    def test_add_blocked_once_at_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_activities = 1
        settings.save()
        TripActivity.objects.create(trip=self.trip, added_by=self.profile, title="Existing", order=0)

        resp = self.client.post(self._url(), data=json.dumps({"title": "New Activity"}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TripActivity.objects.filter(trip=self.trip, title="New Activity").exists())

    def test_add_allowed_under_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_activities = 5
        settings.save()

        resp = self.client.post(self._url(), data=json.dumps({"title": "New Activity"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TripActivity.objects.filter(trip=self.trip, title="New Activity").exists())

    def test_zero_means_unlimited(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_activities = 0
        settings.save()
        for i in range(5):
            TripActivity.objects.create(trip=self.trip, added_by=self.profile, title=f"Activity {i}", order=i)

        resp = self.client.post(self._url(), data=json.dumps({"title": "One More"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)


class SiteAdminFormTests(TestCase):
    """The site-admin settings page renders and persists the six new limit fields."""

    def setUp(self) -> None:
        super().setUp()
        self.admin_user = baker.make(User, username="founder")
        self.client = Client()
        self.client.force_login(self.admin_user)

    def test_page_renders_the_new_fields(self) -> None:
        resp = self.client.get(reverse("site_admin"))
        self.assertEqual(resp.status_code, 200)
        for field in (
            "max_trip_activities",
            "max_upcoming_trips_per_user",
            "max_pins_per_list",
            "max_friends_per_user",
            "max_group_chat_members",
            "max_safety_checkin_contacts",
        ):
            self.assertContains(resp, field)

    def test_post_persists_the_new_fields(self) -> None:
        self.client.post(
            reverse("site_admin"),
            data={
                "max_trip_activities": "42",
                "max_upcoming_trips_per_user": "7",
                "max_pins_per_list": "13",
                "max_friends_per_user": "99",
                "max_group_chat_members": "3",
                "max_safety_checkin_contacts": "2",
            },
        )
        settings = SiteSettings.get_current()
        self.assertEqual(settings.max_trip_activities, 42)
        self.assertEqual(settings.max_upcoming_trips_per_user, 7)
        self.assertEqual(settings.max_pins_per_list, 13)
        self.assertEqual(settings.max_friends_per_user, 99)
        self.assertEqual(settings.max_group_chat_members, 3)
        self.assertEqual(settings.max_safety_checkin_contacts, 2)

    def test_post_clamps_negative_values_to_zero(self) -> None:
        self.client.post(reverse("site_admin"), data={"max_pins_per_list": "-5"})
        settings = SiteSettings.get_current()
        self.assertEqual(settings.max_pins_per_list, 0)


class MaxSafetyCheckinContactsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make(User).profile

    def test_extra_contacts_rejected_past_the_cap(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_safety_checkin_contacts = 2
        settings.save()

        contacts = [(None, f"contact{i}@example.com", f"Contact {i}") for i in range(4)]
        allowed, rejected = validate_notifiable_contacts(self.owner, contacts)
        self.assertEqual(len(allowed), 2)
        self.assertEqual(len(rejected), 2)

    def test_zero_means_unlimited(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_safety_checkin_contacts = 0
        settings.save()

        contacts = [(None, f"contact{i}@example.com", f"Contact {i}") for i in range(6)]
        allowed, rejected = validate_notifiable_contacts(self.owner, contacts)
        self.assertEqual(len(allowed), 6)
        self.assertEqual(len(rejected), 0)
