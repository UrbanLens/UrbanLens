"""The two identity batch paths must agree with ``can_view_profile``, row by row.

``visible_profile_pks`` shows many people to one viewer; ``viewers_who_can_see``
shows one person to many viewers. They are mirror images, and both exist purely
for speed.

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

The mixed-list tests are the ones that matter most: a batching bug is far more
likely to smear one row's answer across its neighbours than to get a
single-row list wrong.
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
from urbanlens.dashboard.models.place.model import Place, PlaceKind
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

    def test_common_pin_across_different_locations_sharing_a_place(self) -> None:
        """A pin fifty metres away on the same parcel must still count as
        "common pin" - the same fix already proven in
        services.pins.common_pins.pinned_place_keys, now shared by
        _have_common_pin/visible_profile_pks/viewers_who_can_see instead of
        each comparing raw Location rows. See docs/GOALS_CODE_AUDIT.md
        ("Cross-pin aggregate comparison level")."""
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        viewer_location = baker.make(Location, place=place)
        baker.make(Pin, profile=self.viewer, location=viewer_location, parent_pin=None)
        with_common = _profile(profile_visibility=VisibilityChoice.COMMON_PIN)
        other_location_same_place = baker.make(Location, place=place)
        self.assertNotEqual(viewer_location.pk, other_location_same_place.pk)
        baker.make(Pin, profile=with_common, location=other_location_same_place, parent_pin=None)
        without = _profile(profile_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=without, location=baker.make(Location, place=baker.make(Place, kind=PlaceKind.PARCEL)), parent_pin=None)

        self._assert_agrees([with_common, without])
        self.assertTrue(with_common.can_view_profile(self.viewer), "different Location rows sharing a Place must count as a common pin")
        self.assertIn(with_common.pk, Profile.visible_profile_pks(self.viewer, [with_common, without]))

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


class ViewersWhoCanSeeAgreementTests(TestCase):
    """The mirror: one subject, many viewers, same contract.

    Written because a group message carries its sender's name, so the name has
    to pass every recipient's own visibility - a question ``visible_profile_pks``
    cannot batch, because it batches over subjects. Resolving it the other way
    round cost a query per member, twice per send.

    Held to ``can_view_profile`` for the same reason as its mirror, and more
    sharply: this one decides whether a *whole room* sees a name, so a
    divergence is not one leaked identity but every recipient at once.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.subject = _profile()

    def _assert_agrees(self, viewers: list[Profile], *, subject: Profile | None = None) -> None:
        """The whole contract: batch == per-viewer, for this exact set."""
        shown = self.subject if subject is None else subject
        batch = Profile.viewers_who_can_see(shown, viewers)
        assert_agrees(
            lambda viewer: shown.can_view_profile(viewer),
            lambda viewer: viewer.pk in batch,
            viewers,
            describe=lambda viewer: f"subject_visibility={shown.profile_visibility!r} viewer={viewer.pk}",
            label="viewers_who_can_see",
        )

    def _set_visibility(self, value: str) -> None:
        Profile.objects.filter(pk=self.subject.pk).update(profile_visibility=value)
        self.subject.refresh_from_db()

    def test_every_visibility_with_no_relationship(self) -> None:
        viewers = [_profile() for _ in VisibilityChoice.values]
        for value in VisibilityChoice.values:
            with self.subTest(visibility=value):
                self._set_visibility(value)
                self._assert_agrees(viewers)

    def test_every_visibility_with_an_accepted_friendship(self) -> None:
        friend = _profile()
        Friendship.objects.create(from_profile=self.subject, to_profile=friend, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        for value in VisibilityChoice.values:
            with self.subTest(visibility=value):
                self._set_visibility(value)
                self._assert_agrees([friend])

    def test_a_request_the_subject_sent_is_directional(self) -> None:
        """The courtesy runs one way: asking to connect reveals who is asking."""
        self._set_visibility(VisibilityChoice.FRIENDS)
        asked = _profile()
        Friendship.objects.create(from_profile=self.subject, to_profile=asked, status=FriendshipStatus.REQUESTED, relationship_type=FriendshipType.FRIEND)
        asker = _profile()
        Friendship.objects.create(from_profile=asker, to_profile=self.subject, status=FriendshipStatus.REQUESTED, relationship_type=FriendshipType.FRIEND)

        self._assert_agrees([asked, asker])
        self.assertEqual(Profile.viewers_who_can_see(self.subject, [asked, asker]), {asked.pk})

    def test_common_pin(self) -> None:
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        shared = baker.make(Location)
        baker.make(Pin, profile=self.subject, location=shared, parent_pin=None)
        with_common = _profile()
        baker.make(Pin, profile=with_common, location=shared, parent_pin=None)
        without = _profile()
        baker.make(Pin, profile=without, location=baker.make(Location), parent_pin=None)

        self._assert_agrees([with_common, without])

    def test_common_pin_across_different_locations_sharing_a_place(self) -> None:
        """Mirror of VisibleProfilePksAgreementTests's test of the same name -
        the subject-side (viewers_who_can_see) batch path must be place-aware too."""
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        subject_location = baker.make(Location, place=place)
        baker.make(Pin, profile=self.subject, location=subject_location, parent_pin=None)
        with_common = _profile()
        other_location_same_place = baker.make(Location, place=place)
        self.assertNotEqual(subject_location.pk, other_location_same_place.pk)
        baker.make(Pin, profile=with_common, location=other_location_same_place, parent_pin=None)
        without = _profile()
        baker.make(Pin, profile=without, location=baker.make(Location, place=baker.make(Place, kind=PlaceKind.PARCEL)), parent_pin=None)

        self._assert_agrees([with_common, without])
        self.assertTrue(self.subject.can_view_profile(with_common), "different Location rows sharing a Place must count as a common pin")
        self.assertIn(with_common.pk, Profile.viewers_who_can_see(self.subject, [with_common, without]))

    def test_common_friend(self) -> None:
        self._set_visibility(VisibilityChoice.COMMON_FRIEND)
        mutual = _profile()
        Friendship.objects.create(from_profile=self.subject, to_profile=mutual, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        with_common = _profile()
        Friendship.objects.create(from_profile=with_common, to_profile=mutual, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        without = _profile()

        self._assert_agrees([with_common, without])

    def test_common_trip(self) -> None:
        self._set_visibility(VisibilityChoice.COMMON_TRIP)
        trip = baker.make(Trip, creator=self.subject)
        TripMembership.objects.create(trip=trip, profile=self.subject)
        with_common = _profile()
        TripMembership.objects.create(trip=trip, profile=with_common)
        without = _profile()

        self._assert_agrees([with_common, without])

    def test_anything_in_common_via_each_route(self) -> None:
        self._set_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        shared_location = baker.make(Location)
        baker.make(Pin, profile=self.subject, location=shared_location, parent_pin=None)
        mutual = _profile()
        Friendship.objects.create(from_profile=self.subject, to_profile=mutual, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        trip = baker.make(Trip, creator=self.subject)
        TripMembership.objects.create(trip=trip, profile=self.subject)

        by_pin = _profile()
        baker.make(Pin, profile=by_pin, location=shared_location, parent_pin=None)
        by_friend = _profile()
        Friendship.objects.create(from_profile=by_friend, to_profile=mutual, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        by_trip = _profile()
        TripMembership.objects.create(trip=trip, profile=by_trip)
        by_nothing = _profile()

        self._assert_agrees([by_pin, by_friend, by_trip, by_nothing])

    def test_a_temporary_grant_is_honoured_and_a_block_vetoes_it(self) -> None:
        """NO_ONE skips every gate and must still reach the grant."""
        from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess

        self._set_visibility(VisibilityChoice.NO_ONE)
        granted = _profile()
        DirectMessageTemporaryAccess.objects.create(profile=self.subject, granted_to=granted, expires_at=timezone.now() + datetime.timedelta(days=1))
        blocked = _profile()
        DirectMessageTemporaryAccess.objects.create(profile=self.subject, granted_to=blocked, expires_at=timezone.now() + datetime.timedelta(days=1))
        Friendship.objects.create(from_profile=self.subject, to_profile=blocked, status=FriendshipStatus.BLOCKED, relationship_type=FriendshipType.FRIEND)
        expired = _profile()
        DirectMessageTemporaryAccess.objects.create(profile=self.subject, granted_to=expired, expires_at=timezone.now() - datetime.timedelta(days=1))

        self._assert_agrees([granted, blocked, expired])

    def test_the_subject_always_sees_themselves(self) -> None:
        """Checked before the visibility setting, as can_view_profile does."""
        self._set_visibility(VisibilityChoice.NO_ONE)

        self._assert_agrees([self.subject])
        self.assertEqual(Profile.viewers_who_can_see(self.subject, [self.subject]), {self.subject.pk})

    def test_a_mixed_room_does_not_smear_one_viewers_answer_onto_another(self) -> None:
        """The failure mode batching actually has, and here it is a whole room."""
        self._set_visibility(VisibilityChoice.COMMON_PIN)
        shared = baker.make(Location)
        baker.make(Pin, profile=self.subject, location=shared, parent_pin=None)

        friend = _profile()
        Friendship.objects.create(from_profile=self.subject, to_profile=friend, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        pin_mate = _profile()
        baker.make(Pin, profile=pin_mate, location=shared, parent_pin=None)
        stranger = _profile()
        pin_stranger = _profile()
        baker.make(Pin, profile=pin_stranger, location=baker.make(Location), parent_pin=None)

        viewers = [friend, pin_mate, stranger, pin_stranger, self.subject]
        self._assert_agrees(viewers)
        self.assertEqual(
            Profile.viewers_who_can_see(self.subject, viewers),
            {friend.pk, pin_mate.pk, self.subject.pk},
            "the visible set is not the expected one - a neighbour's relationship leaked",
        )

    def test_a_public_subject_is_visible_to_everyone_without_a_query(self) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._set_visibility(VisibilityChoice.ANYONE)
        viewers = [_profile() for _ in range(3)]

        with CaptureQueriesContext(connection) as queries:
            batch = Profile.viewers_who_can_see(self.subject, viewers)

        self.assertEqual(batch, {viewer.pk for viewer in viewers})
        self.assertEqual(len(queries.captured_queries), 0)

    def test_an_empty_viewer_list_is_not_a_query(self) -> None:
        self.assertEqual(Profile.viewers_who_can_see(self.subject, []), set())

    def test_the_lookup_count_does_not_grow_with_the_room(self) -> None:
        """The whole point: one resolution for a group of any size."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._set_visibility(VisibilityChoice.ANYTHING_IN_COMMON)
        # Built outside the capture: creating a profile is itself a dozen
        # queries, which would swamp the thing being measured.
        few_viewers = [_profile() for _ in range(2)]
        many_viewers = [_profile() for _ in range(12)]

        with CaptureQueriesContext(connection) as few:
            Profile.viewers_who_can_see(self.subject, few_viewers)
        with CaptureQueriesContext(connection) as many:
            Profile.viewers_who_can_see(self.subject, many_viewers)

        self.assertEqual(len(many.captured_queries), len(few.captured_queries))
