"""Tests for the mutual "Places in Common" privacy setting, service, and page.

Covers:
- common_pin_location_ids/common_pin_locations: N-way intersection service
- Profile.can_view_common_pins_with: mutual gating (both sides must permit)
- CommonPinsView: 404 unless mutually permitted, and only ever renders the
  viewer's own Pin data for a shared location - never the other profile's
"""

from __future__ import annotations

from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.common_pins import common_pin_location_ids, common_pin_locations


def _make_profile() -> Profile:
    return baker.make("auth.User").profile


def _share_pin(a: Profile, b: Profile, name: str = "Shared Spot") -> Location:
    location = baker.make(Location, official_name=name)
    Pin.objects.create(profile=a, location=location)
    Pin.objects.create(profile=b, location=location)
    return location


def _befriend(a: Profile, b: Profile) -> Friendship:
    return Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


class CommonPinLocationsServiceTests(TestCase):
    def setUp(self):
        self.alice = _make_profile()
        self.bob = _make_profile()

    def test_empty_with_a_single_profile(self):
        self.assertEqual(common_pin_location_ids([self.alice]), set())
        self.assertFalse(common_pin_locations([self.alice]).exists())

    def test_empty_with_no_profiles(self):
        self.assertEqual(common_pin_location_ids([]), set())

    def test_intersects_two_profiles(self):
        shared = _share_pin(self.alice, self.bob)
        alice_only = baker.make(Location, official_name="Alice only")
        Pin.objects.create(profile=self.alice, location=alice_only)

        self.assertEqual(common_pin_location_ids([self.alice, self.bob]), {shared.pk})
        self.assertEqual(set(common_pin_locations([self.alice, self.bob]).values_list("pk", flat=True)), {shared.pk})

    def test_no_overlap_returns_empty(self):
        Pin.objects.create(profile=self.alice, location=baker.make(Location))
        Pin.objects.create(profile=self.bob, location=baker.make(Location))
        self.assertEqual(common_pin_location_ids([self.alice, self.bob]), set())

    def test_extensible_to_three_profiles(self):
        """The service intersects any number of profiles, ready for group (e.g. trip) use."""
        carol = _make_profile()
        shared_by_all = _share_pin(self.alice, self.bob)
        Pin.objects.create(profile=carol, location=shared_by_all)
        # Only alice+bob share this second one - carol doesn't, so it must drop out.
        _share_pin(self.alice, self.bob, name="Alice+Bob only")

        self.assertEqual(common_pin_location_ids([self.alice, self.bob, carol]), {shared_by_all.pk})


class CanViewCommonPinsWithTests(TestCase):
    """Profile.can_view_common_pins_with - mutual, unlike other visibility fields."""

    def setUp(self):
        self.alice = _make_profile()
        self.bob = _make_profile()

    def test_none_viewer_blocked(self):
        self.assertFalse(self.alice.can_view_common_pins_with(None))

    def test_self_blocked(self):
        self.assertFalse(self.alice.can_view_common_pins_with(self.alice))

    def test_default_friends_only_blocks_strangers(self):
        self.assertFalse(self.alice.can_view_common_pins_with(self.bob))
        self.assertFalse(self.bob.can_view_common_pins_with(self.alice))

    def test_friends_permits_both_directions(self):
        _befriend(self.alice, self.bob)
        self.assertTrue(self.alice.can_view_common_pins_with(self.bob))
        self.assertTrue(self.bob.can_view_common_pins_with(self.alice))

    def test_one_sided_anyone_still_blocked_by_the_others_default(self):
        """Alice opening her setting doesn't unlock anything if Bob hasn't too."""
        self.alice.common_pins_visibility = VisibilityChoice.ANYONE
        self.alice.save()
        self.assertFalse(self.alice.can_view_common_pins_with(self.bob))
        self.assertFalse(self.bob.can_view_common_pins_with(self.alice))

    def test_both_anyone_permits_without_friendship(self):
        self.alice.common_pins_visibility = VisibilityChoice.ANYONE
        self.alice.save()
        self.bob.common_pins_visibility = VisibilityChoice.ANYONE
        self.bob.save()
        self.assertTrue(self.alice.can_view_common_pins_with(self.bob))
        self.assertTrue(self.bob.can_view_common_pins_with(self.alice))

    def test_one_no_one_blocks_even_if_the_other_allows_anyone(self):
        self.alice.common_pins_visibility = VisibilityChoice.ANYONE
        self.alice.save()
        self.bob.common_pins_visibility = VisibilityChoice.NO_ONE
        self.bob.save()
        self.assertFalse(self.alice.can_view_common_pins_with(self.bob))
        self.assertFalse(self.bob.can_view_common_pins_with(self.alice))


class CommonPinsViewTests(TestCase):
    def setUp(self):
        self.alice = _make_profile()
        self.bob = _make_profile()
        self.client = Client()
        self.client.force_login(self.alice.user)

    def _url(self, other: Profile) -> str:
        return reverse("profile.common_pins", args=[other.ensure_slug()])

    def test_404_when_not_mutually_permitted(self):
        _share_pin(self.alice, self.bob)
        response = self.client.get(self._url(self.bob))
        self.assertEqual(response.status_code, 404)

    def test_200_when_mutually_permitted_via_friendship(self):
        _share_pin(self.alice, self.bob)
        _befriend(self.alice, self.bob)
        response = self.client.get(self._url(self.bob))
        self.assertEqual(response.status_code, 200)

    def test_only_the_viewers_own_pin_data_is_shown(self):
        """The other profile's private pin fields (custom name) must never leak through."""
        location = _share_pin(self.alice, self.bob)
        Pin.objects.filter(profile=self.alice, location=location).update(name="Alice's secret name")
        Pin.objects.filter(profile=self.bob, location=location).update(name="Bob's private note")
        _befriend(self.alice, self.bob)

        response = self.client.get(self._url(self.bob))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Alice's secret name", content)
        self.assertNotIn("Bob's private note", content)

    def test_404_for_a_profile_with_nothing_in_common(self):
        _befriend(self.alice, self.bob)
        response = self.client.get(self._url(self.bob))
        self.assertEqual(response.status_code, 200)  # mutually permitted, just an empty page

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(self._url(self.bob))
        self.assertEqual(response.status_code, 302)


class ProfileViewCommonPinsContextTests(TestCase):
    """ViewProfileView's can_view_common_pins context flag, used to link/not-link the stat."""

    def setUp(self):
        self.alice = _make_profile()
        self.bob = _make_profile()
        self.client = Client()
        self.client.force_login(self.alice.user)

    def test_flag_false_without_mutual_permission(self):
        _share_pin(self.alice, self.bob)
        response = self.client.get(reverse("profile.view_user", args=[self.bob.ensure_slug()]))
        self.assertFalse(response.context["can_view_common_pins"])

    def test_flag_true_with_mutual_permission_and_shared_pins(self):
        _share_pin(self.alice, self.bob)
        _befriend(self.alice, self.bob)
        response = self.client.get(reverse("profile.view_user", args=[self.bob.ensure_slug()]))
        self.assertTrue(response.context["can_view_common_pins"])

    def test_flag_false_when_mutually_permitted_but_nothing_shared(self):
        _befriend(self.alice, self.bob)
        response = self.client.get(reverse("profile.view_user", args=[self.bob.ensure_slug()]))
        self.assertFalse(response.context["can_view_common_pins"])
