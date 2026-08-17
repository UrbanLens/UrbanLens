"""``visible_profile_pks`` must agree with ``can_view_profile``, subject by subject.

The batch path exists only for speed: rendering a list of people called
``can_view_profile`` once per row, and every relationship helper it reaches
rebuilt the *viewer's* own sets (pinned locations, accepted friends, trip ids)
on each call.

Speed is not worth a divergence here. If the batch answers "visible" where the
single-subject path answers "masked", a real name and avatar appear for someone
the viewer has no standing right to identify. So these tests do not check the
batch against hand-written expectations - they check it against
``can_view_profile`` itself, across every ``VisibilityChoice`` and every
relationship that can satisfy one, and assert the two agree exactly.

The mixed-list test is the one that matters most: a batching bug is far more
likely to smear one subject's answer across its neighbours than to get a
single-subject list wrong.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership


def _profile(**kwargs) -> Profile:
    profile = baker.make(User).profile
    if kwargs:
        Profile.objects.filter(pk=profile.pk).update(**kwargs)
        profile.refresh_from_db()
    return profile


class VisibleProfilePksAgreementTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()

    def _assert_agrees(self, subjects: list[Profile], *, viewer: Profile | None = None) -> None:
        """The whole contract: batch == per-subject, for this exact set."""
        looker = self.viewer if viewer is None else viewer
        batch = Profile.visible_profile_pks(looker, subjects)
        assert_agrees(
            lambda subject: subject.can_view_profile(looker),
            lambda subject: subject.pk in batch,
            subjects,
            describe=lambda subject: f"profile_visibility={subject.profile_visibility!r}",
            label="visible_profile_pks",
        )

    def _befriend(self, other: Profile) -> None:
        Friendship.objects.create(
            from_profile=self.viewer,
            to_profile=other,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )

    def test_every_visibility_with_no_relationship(self) -> None:
        subjects = [_profile(profile_visibility=value) for value in VisibilityChoice.values]

        self._assert_agrees(subjects)

    def test_every_visibility_with_an_accepted_friendship(self) -> None:
        subjects = [_profile(profile_visibility=value) for value in VisibilityChoice.values]
        for subject in subjects:
            self._befriend(subject)

        self._assert_agrees(subjects)

    def test_a_request_the_subject_sent_is_directional(self) -> None:
        """has_pending_request_to(subject, viewer) - the reverse must not count."""
        sent_to_viewer = _profile(profile_visibility=VisibilityChoice.FRIENDS)
        Friendship.objects.create(from_profile=sent_to_viewer, to_profile=self.viewer, status=FriendshipStatus.REQUESTED, relationship_type=FriendshipType.FRIEND)
        sent_by_viewer = _profile(profile_visibility=VisibilityChoice.FRIENDS)
        Friendship.objects.create(from_profile=self.viewer, to_profile=sent_by_viewer, status=FriendshipStatus.REQUESTED, relationship_type=FriendshipType.FRIEND)

        self._assert_agrees([sent_to_viewer, sent_by_viewer])

    def test_common_pin(self) -> None:
        shared = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared, parent_pin=None)
        with_common = _profile(profile_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=with_common, location=shared, parent_pin=None)
        without = _profile(profile_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=without, location=baker.make(Location), parent_pin=None)

        self._assert_agrees([with_common, without])

    def test_common_friend(self) -> None:
        mutual = _profile()
        self._befriend(mutual)
        with_common = _profile(profile_visibility=VisibilityChoice.COMMON_FRIEND)
        Friendship.objects.create(from_profile=with_common, to_profile=mutual, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        without = _profile(profile_visibility=VisibilityChoice.COMMON_FRIEND)

        self._assert_agrees([with_common, without])

    def test_common_trip(self) -> None:
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer)
        with_common = _profile(profile_visibility=VisibilityChoice.COMMON_TRIP)
        TripMembership.objects.create(trip=trip, profile=with_common)
        without = _profile(profile_visibility=VisibilityChoice.COMMON_TRIP)

        self._assert_agrees([with_common, without])

    def test_anything_in_common_via_each_route(self) -> None:
        shared_location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared_location, parent_pin=None)
        mutual = _profile()
        self._befriend(mutual)
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer)

        by_pin = _profile(profile_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        baker.make(Pin, profile=by_pin, location=shared_location, parent_pin=None)
        by_friend = _profile(profile_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        Friendship.objects.create(from_profile=by_friend, to_profile=mutual, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        by_trip = _profile(profile_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        TripMembership.objects.create(trip=trip, profile=by_trip)
        by_nothing = _profile(profile_visibility=VisibilityChoice.ANYTHING_IN_COMMON)

        self._assert_agrees([by_pin, by_friend, by_trip, by_nothing])

    def test_a_temporary_grant_is_honoured_and_a_block_vetoes_it(self) -> None:
        from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess

        granted = _profile(profile_visibility=VisibilityChoice.NO_ONE)
        DirectMessageTemporaryAccess.objects.create(profile=granted, granted_to=self.viewer, expires_at=timezone.now() + datetime.timedelta(days=1))
        blocked = _profile(profile_visibility=VisibilityChoice.NO_ONE)
        DirectMessageTemporaryAccess.objects.create(profile=blocked, granted_to=self.viewer, expires_at=timezone.now() + datetime.timedelta(days=1))
        Friendship.objects.create(from_profile=blocked, to_profile=self.viewer, status=FriendshipStatus.BLOCKED, relationship_type=FriendshipType.FRIEND)
        expired = _profile(profile_visibility=VisibilityChoice.NO_ONE)
        DirectMessageTemporaryAccess.objects.create(profile=expired, granted_to=self.viewer, expires_at=timezone.now() - datetime.timedelta(days=1))

        self._assert_agrees([granted, blocked, expired])

    def test_a_mixed_list_does_not_smear_one_subjects_answer_onto_another(self) -> None:
        """The failure mode batching actually has: neighbours contaminating each other."""
        shared = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared, parent_pin=None)

        friend = _profile(profile_visibility=VisibilityChoice.FRIENDS)
        self._befriend(friend)
        stranger = _profile(profile_visibility=VisibilityChoice.FRIENDS)
        pin_mate = _profile(profile_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=pin_mate, location=shared, parent_pin=None)
        pin_stranger = _profile(profile_visibility=VisibilityChoice.COMMON_PIN)
        anyone = _profile(profile_visibility=VisibilityChoice.ANYONE)
        no_one = _profile(profile_visibility=VisibilityChoice.NO_ONE)

        subjects = [friend, stranger, pin_mate, pin_stranger, anyone, no_one, self.viewer]
        self._assert_agrees(subjects)

        batch = Profile.visible_profile_pks(self.viewer, subjects)
        self.assertEqual(
            batch,
            {friend.pk, pin_mate.pk, anyone.pk, self.viewer.pk},
            "the visible set is not the expected one - a neighbour's relationship leaked",
        )

    def test_an_anonymous_viewer_sees_only_public_profiles(self) -> None:
        subjects = [_profile(profile_visibility=value) for value in VisibilityChoice.values]

        self._assert_agrees(subjects, viewer=None)

    def test_an_empty_subject_list_is_not_a_query(self) -> None:
        self.assertEqual(Profile.visible_profile_pks(self.viewer, []), set())
