"""Tests for the "Users with anything in common" visibility option and the
friends-always-qualify rule.

Covers:
- VisibilityChoice contains ANYTHING_IN_COMMON and is ordered least → most restrictive
- New-profile defaults changed from ANYONE to ANYTHING_IN_COMMON
- Accepted friends qualify for every relationship-based visibility option
  (everything except NO_ONE) across profile, contact, image, trip-activity,
  and friend-request checks
- ANYTHING_IN_COMMON permits users sharing a pin, a friend, or a trip - or who
  are already friends - and blocks complete strangers
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.trip import _apply_trip_visibility_filter
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership

# Every option a friend must pass (everything except NO_ONE; ANYONE is trivial).
_FRIEND_QUALIFYING_CHOICES = (
    VisibilityChoice.ANYONE,
    VisibilityChoice.ANYTHING_IN_COMMON,
    VisibilityChoice.COMMON_PIN,
    VisibilityChoice.COMMON_FRIEND,
    VisibilityChoice.COMMON_TRIP,
    VisibilityChoice.FRIENDS,
)


def _make_profile() -> Profile:
    return baker.make("auth.User").profile


def _befriend(a: Profile, b: Profile) -> Friendship:
    return Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


def _share_pin(a: Profile, b: Profile) -> Location:
    location = baker.make(Location, official_name="Shared Spot")
    Pin.objects.create(profile=a, location=location)
    Pin.objects.create(profile=b, location=location)
    return location


def _share_trip(a: Profile, b: Profile) -> Trip:
    trip = Trip.objects.create(name="Shared Trip", creator=a)
    TripMembership.objects.create(trip=trip, profile=a)
    TripMembership.objects.create(trip=trip, profile=b)
    return trip


def _share_friend(a: Profile, b: Profile) -> Profile:
    mutual = _make_profile()
    _befriend(a, mutual)
    _befriend(mutual, b)
    return mutual


class VisibilityChoiceEnumTests(TestCase):
    """The enum contains the new option and lists choices least → most restrictive."""

    def test_anything_in_common_exists(self) -> None:
        self.assertEqual(VisibilityChoice.ANYTHING_IN_COMMON.value, "anything_in_common")

    def test_choices_ordered_least_to_most_restrictive(self) -> None:
        self.assertEqual(
            list(VisibilityChoice.values),
            [
                "anyone",
                "anything_in_common",
                "common_pin",
                "common_friend",
                "common_trip",
                "friends",
                "no_one",
            ],
        )


class VisibilityDefaultsTests(TestCase):
    """New profiles default to ANYTHING_IN_COMMON where they previously used ANYONE."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _make_profile()

    def test_profile_visibility_default(self) -> None:
        self.assertEqual(self.profile.profile_visibility, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_comment_visibility_default(self) -> None:
        self.assertEqual(self.profile.comment_visibility, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_friend_request_visibility_default(self) -> None:
        self.assertEqual(self.profile.friend_request_visibility, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_photo_upload_visibility_default(self) -> None:
        self.assertEqual(self.profile.photo_upload_visibility, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_viewer_photo_filter_default(self) -> None:
        self.assertEqual(self.profile.viewer_photo_filter, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_trip_pin_location_visibility_default(self) -> None:
        self.assertEqual(self.profile.trip_pin_location_visibility, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_contact_visibility_still_defaults_to_friends(self) -> None:
        self.assertEqual(self.profile.contact_visibility, VisibilityChoice.FRIENDS)


class FriendsQualifyForProfileVisibilityTests(TestCase):
    """An accepted friend passes every visibility option except NO_ONE."""

    def setUp(self) -> None:
        super().setUp()
        self.subject = _make_profile()
        self.friend = _make_profile()
        _befriend(self.subject, self.friend)

    def test_friend_passes_every_qualifying_option(self) -> None:
        for visibility in _FRIEND_QUALIFYING_CHOICES:
            with self.subTest(visibility=visibility):
                self.subject.profile_visibility = visibility
                self.subject.save(update_fields=["profile_visibility"])
                self.assertTrue(self.subject.can_view_profile(self.friend))

    def test_friend_blocked_by_no_one(self) -> None:
        self.subject.profile_visibility = VisibilityChoice.NO_ONE
        self.subject.save(update_fields=["profile_visibility"])
        self.assertFalse(self.subject.can_view_profile(self.friend))

    def test_friend_passes_contact_visibility_options(self) -> None:
        for visibility in _FRIEND_QUALIFYING_CHOICES:
            with self.subTest(visibility=visibility):
                self.subject.contact_visibility = visibility
                self.subject.save(update_fields=["contact_visibility"])
                self.assertTrue(self.subject.can_view_contact_info(self.friend))

    def test_stranger_still_blocked_by_common_pin(self) -> None:
        stranger = _make_profile()
        self.subject.profile_visibility = VisibilityChoice.COMMON_PIN
        self.subject.save(update_fields=["profile_visibility"])
        self.assertFalse(self.subject.can_view_profile(stranger))


class AnythingInCommonProfileTests(TestCase):
    """ANYTHING_IN_COMMON permits any one of: shared pin, shared friend, shared trip, or friendship."""

    def setUp(self) -> None:
        super().setUp()
        self.subject = _make_profile()
        self.viewer = _make_profile()
        self.subject.profile_visibility = VisibilityChoice.ANYTHING_IN_COMMON
        self.subject.save(update_fields=["profile_visibility"])

    def test_stranger_is_blocked(self) -> None:
        self.assertFalse(self.subject.can_view_profile(self.viewer))

    def test_shared_pin_qualifies(self) -> None:
        _share_pin(self.subject, self.viewer)
        self.assertTrue(self.subject.can_view_profile(self.viewer))

    def test_shared_trip_qualifies(self) -> None:
        _share_trip(self.subject, self.viewer)
        self.assertTrue(self.subject.can_view_profile(self.viewer))

    def test_shared_friend_qualifies(self) -> None:
        _share_friend(self.subject, self.viewer)
        self.assertTrue(self.subject.can_view_profile(self.viewer))

    def test_friendship_qualifies(self) -> None:
        _befriend(self.subject, self.viewer)
        self.assertTrue(self.subject.can_view_profile(self.viewer))


class ImageVisibilityFriendTests(TestCase):
    """Friends see each other's photos under every uploader setting except NO_ONE."""

    def setUp(self) -> None:
        super().setUp()
        self.uploader = _make_profile()
        self.viewer = _make_profile()
        # The viewer's own filter must not interfere with these tests.
        self.viewer.viewer_photo_filter = VisibilityChoice.ANYONE
        self.viewer.save(update_fields=["viewer_photo_filter"])
        self.image = baker.make("dashboard.Image", profile=self.uploader, pin=None, wiki=None)

    def _set_upload_visibility(self, visibility: str) -> None:
        self.uploader.photo_upload_visibility = visibility
        self.uploader.save(update_fields=["photo_upload_visibility"])

    def test_friend_sees_photos_under_every_qualifying_option(self) -> None:
        _befriend(self.uploader, self.viewer)
        for visibility in _FRIEND_QUALIFYING_CHOICES:
            with self.subTest(visibility=visibility):
                self._set_upload_visibility(visibility)
                self.assertIn(self.image, Image.objects.visible_to(self.viewer))

    def test_stranger_blocked_by_anything_in_common(self) -> None:
        self._set_upload_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        self.assertNotIn(self.image, Image.objects.visible_to(self.viewer))

    def test_shared_trip_qualifies_for_anything_in_common(self) -> None:
        self._set_upload_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        _share_trip(self.uploader, self.viewer)
        self.assertIn(self.image, Image.objects.visible_to(self.viewer))

    def test_viewer_filter_anything_in_common_respects_friendship(self) -> None:
        self._set_upload_visibility(VisibilityChoice.ANYONE)
        self.viewer.viewer_photo_filter = VisibilityChoice.ANYTHING_IN_COMMON
        self.viewer.save(update_fields=["viewer_photo_filter"])
        self.assertNotIn(self.image, Image.objects.visible_to(self.viewer))
        _befriend(self.uploader, self.viewer)
        self.assertIn(self.image, Image.objects.visible_to(self.viewer))


class TripActivityVisibilityFriendTests(TestCase):
    """Friends of the adder see activity locations for every option except NO_ONE."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = _make_profile()
        self.adder = _make_profile()
        _befriend(self.viewer, self.adder)
        self.location = baker.make(Location, official_name="Old Foundry")
        self.trip = Trip.objects.create(name="Filter Trip", creator=self.adder)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)

    def _make_activity(self, visibility: str) -> TripActivity:
        self.adder.trip_pin_location_visibility = visibility
        self.adder.save(update_fields=["trip_pin_location_visibility"])
        return TripActivity.objects.create(trip=self.trip, added_by=self.adder, location=self.location, title="Act")

    def test_friend_sees_common_pin_activity_without_shared_pin(self) -> None:
        act = self._make_activity(VisibilityChoice.COMMON_PIN)
        hidden: set[int] = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_friend_sees_common_friend_activity_without_mutual_friend(self) -> None:
        act = self._make_activity(VisibilityChoice.COMMON_FRIEND)
        hidden: set[int] = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_anything_in_common_activity_visible_to_trip_member(self) -> None:
        act = self._make_activity(VisibilityChoice.ANYTHING_IN_COMMON)
        hidden: set[int] = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertNotIn(act.id, hidden)

    def test_no_one_activity_still_hidden_from_friend(self) -> None:
        act = self._make_activity(VisibilityChoice.NO_ONE)
        hidden: set[int] = set()
        _apply_trip_visibility_filter([act], self.viewer, hidden)
        self.assertIn(act.id, hidden)


class FriendRequestVisibilityTests(TestCase):
    """Friend-request gate honours ANYTHING_IN_COMMON and the friends-qualify rule."""

    def setUp(self) -> None:
        super().setUp()
        self.requester_user = baker.make("auth.User")
        self.requester = self.requester_user.profile
        self.target = _make_profile()
        self.client.force_login(self.requester_user)

    def _request_friend(self):
        return self.client.post(reverse("friend.request", kwargs={"profile_id": self.target.pk}))

    def _set_visibility(self, visibility: str) -> None:
        self.target.friend_request_visibility = visibility
        self.target.save(update_fields=["friend_request_visibility"])

    def test_anything_in_common_blocks_stranger(self) -> None:
        self._set_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        self.assertEqual(self._request_friend().status_code, 403)

    def test_anything_in_common_allows_shared_pin(self) -> None:
        # Non-HTMX success redirects back to the profile page (302).
        self._set_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        _share_pin(self.requester, self.target)
        self.assertEqual(self._request_friend().status_code, 302)

    def test_anything_in_common_allows_shared_trip(self) -> None:
        self._set_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        _share_trip(self.requester, self.target)
        self.assertEqual(self._request_friend().status_code, 302)

    def test_common_trip_gate_admits_existing_friend(self) -> None:
        # A friend passes the COMMON_TRIP visibility gate (no 403); the request
        # then fails the duplicate-friendship rule (400), which is unrelated.
        self._set_visibility(VisibilityChoice.COMMON_TRIP)
        _befriend(self.requester, self.target)
        self.assertNotEqual(self._request_friend().status_code, 403)
        self.assertTrue(Profile.visibility_permits(VisibilityChoice.COMMON_TRIP, self.target, self.requester))

    def test_common_pin_still_blocks_stranger(self) -> None:
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        self.assertEqual(self._request_friend().status_code, 403)


class PendingRequestVisibilityTests(TestCase):
    """An unanswered friend request opens the sender's privacy gates to the recipient - one way only."""

    _RELATIONSHIP_CHOICES = (
        VisibilityChoice.FRIENDS,
        VisibilityChoice.COMMON_PIN,
        VisibilityChoice.COMMON_FRIEND,
        VisibilityChoice.COMMON_TRIP,
        VisibilityChoice.ANYTHING_IN_COMMON,
    )

    def setUp(self) -> None:
        super().setUp()
        self.sender = _make_profile()
        self.recipient = _make_profile()
        self.request_row = Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.REQUESTED)

    def test_recipient_passes_every_relationship_gate_on_sender(self) -> None:
        for choice in self._RELATIONSHIP_CHOICES:
            with self.subTest(choice=choice):
                self.assertTrue(Profile.visibility_permits(choice, self.sender, self.recipient))

    def test_sender_gains_nothing_on_recipient(self) -> None:
        for choice in self._RELATIONSHIP_CHOICES:
            with self.subTest(choice=choice):
                self.assertFalse(Profile.visibility_permits(choice, self.recipient, self.sender))

    def test_recipient_can_view_sender_profile_page(self) -> None:
        self.sender.profile_visibility = VisibilityChoice.FRIENDS
        self.sender.save(update_fields=["profile_visibility"])
        self.assertTrue(self.sender.can_view_profile(self.recipient))
        self.assertFalse(self.recipient.can_view_profile(self.sender))

    def test_no_one_still_blocks_recipient(self) -> None:
        self.assertFalse(Profile.visibility_permits(VisibilityChoice.NO_ONE, self.sender, self.recipient))

    def test_declined_request_grants_nothing(self) -> None:
        self.request_row.status = FriendshipStatus.DECLINED
        self.request_row.save(update_fields=["status"])
        self.assertFalse(Profile.visibility_permits(VisibilityChoice.FRIENDS, self.sender, self.recipient))

    def test_accepted_request_grants_both_ways(self) -> None:
        self.request_row.status = FriendshipStatus.ACCEPTED
        self.request_row.save(update_fields=["status"])
        self.assertTrue(Profile.visibility_permits(VisibilityChoice.FRIENDS, self.sender, self.recipient))
        self.assertTrue(Profile.visibility_permits(VisibilityChoice.FRIENDS, self.recipient, self.sender))
