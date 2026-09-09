"""``visible_contact_info_pks`` must agree with ``can_view_contact_info``, row by row.

The contact-info sibling of ``test_identity_visibility_batch``, and it exists
for the same reason: a batch path is a second implementation of a decision that
already had one, and the two drift silently because each is self-consistent.

Two things separate this field from ``profile_visibility``, and both are the
kind of difference a shared implementation quietly loses:

- an unanswered friend request does **not** open the gate (a phone number is
  more sensitive than "who's asking to connect"), and
- a ``DirectMessageTemporaryAccess`` grant reveals an identity, never a contact
  method.

So the interesting tests here are the two negatives - the pending request and
the temporary grant - because a batch helper parameterised over both fields
passes everything else whether or not it honours them.

``related_profile_ids`` gets its own class. It is a deliberate *superset*, so it
cannot be tested for equality with anything; what it must never do is miss a
profile that a real gate would have passed, which is what each test asserts.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess
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


class VisibleContactInfoPksAgreementTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()

    def _assert_agrees(self, subjects: list[Profile], *, viewer: Profile | None = None) -> None:
        looker = self.viewer if viewer is None else viewer
        batch = Profile.visible_contact_info_pks(looker, subjects)
        assert_agrees(
            lambda subject: subject.can_view_contact_info(looker),
            lambda subject: subject.pk in batch,
            subjects,
            describe=lambda subject: f"contact_visibility={subject.contact_visibility!r}",
            label="visible_contact_info_pks",
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
        subjects = [_profile(contact_visibility=value) for value in VisibilityChoice.values]

        self._assert_agrees(subjects)

    def test_every_visibility_with_an_accepted_friendship(self) -> None:
        subjects = [_profile(contact_visibility=value) for value in VisibilityChoice.values]
        for subject in subjects:
            self._befriend(subject)

        self._assert_agrees(subjects)

    def test_every_visibility_for_an_anonymous_viewer(self) -> None:
        subjects = [_profile(contact_visibility=value) for value in VisibilityChoice.values]

        batch = Profile.visible_contact_info_pks(None, subjects)
        assert_agrees(
            lambda subject: subject.can_view_contact_info(None),
            lambda subject: subject.pk in batch,
            subjects,
            describe=lambda subject: f"contact_visibility={subject.contact_visibility!r}",
            label="visible_contact_info_pks(None)",
        )

    def test_a_pending_request_does_not_open_the_contact_gate(self) -> None:
        """The difference from ``profile_visibility``: an unanswered request is not
        an accepted friendship, and contact details wait for the acceptance."""
        sent_to_viewer = _profile(contact_visibility=VisibilityChoice.FRIENDS)
        Friendship.objects.create(
            from_profile=sent_to_viewer,
            to_profile=self.viewer,
            status=FriendshipStatus.REQUESTED,
            relationship_type=FriendshipType.FRIEND,
        )

        self.assertFalse(sent_to_viewer.can_view_contact_info(self.viewer))
        self._assert_agrees([sent_to_viewer])

    def test_a_temporary_grant_does_not_open_the_contact_gate(self) -> None:
        """A grant reveals an identity. ``can_view_contact_info`` never consults it,
        so a batch path that reached the same fallback would over-share."""
        granted = _profile(contact_visibility=VisibilityChoice.NO_ONE)
        DirectMessageTemporaryAccess.objects.create(
            profile=granted,
            granted_to=self.viewer,
            expires_at=timezone.now() + datetime.timedelta(days=1),
        )

        self.assertFalse(granted.can_view_contact_info(self.viewer))
        self._assert_agrees([granted])

    def test_common_pin(self) -> None:
        shared = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared, parent_pin=None)
        with_common = _profile(contact_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=with_common, location=shared, parent_pin=None)
        without = _profile(contact_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=without, location=baker.make(Location), parent_pin=None)

        self._assert_agrees([with_common, without])

    def test_common_pin_across_different_locations_sharing_a_place(self) -> None:
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        baker.make(Pin, profile=self.viewer, location=baker.make(Location, place=place), parent_pin=None)
        with_common = _profile(contact_visibility=VisibilityChoice.COMMON_PIN)
        baker.make(Pin, profile=with_common, location=baker.make(Location, place=place), parent_pin=None)
        without = _profile(contact_visibility=VisibilityChoice.COMMON_PIN)

        self._assert_agrees([with_common, without])

    def test_common_friend(self) -> None:
        mutual = _profile()
        self._befriend(mutual)
        with_common = _profile(contact_visibility=VisibilityChoice.COMMON_FRIEND)
        Friendship.objects.create(
            from_profile=with_common,
            to_profile=mutual,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )
        without = _profile(contact_visibility=VisibilityChoice.COMMON_FRIEND)

        self._assert_agrees([with_common, without])

    def test_common_trip(self) -> None:
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer)
        with_common = _profile(contact_visibility=VisibilityChoice.COMMON_TRIP)
        TripMembership.objects.create(trip=trip, profile=with_common)
        without = _profile(contact_visibility=VisibilityChoice.COMMON_TRIP)

        self._assert_agrees([with_common, without])

    def test_anything_in_common_via_each_route(self) -> None:
        shared_location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared_location, parent_pin=None)
        mutual = _profile()
        self._befriend(mutual)
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer)

        by_pin = _profile(contact_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        baker.make(Pin, profile=by_pin, location=shared_location, parent_pin=None)
        by_friend = _profile(contact_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        Friendship.objects.create(
            from_profile=by_friend,
            to_profile=mutual,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )
        by_trip = _profile(contact_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        TripMembership.objects.create(trip=trip, profile=by_trip)
        by_nothing = _profile(contact_visibility=VisibilityChoice.ANYTHING_IN_COMMON)

        self._assert_agrees([by_pin, by_friend, by_trip, by_nothing])

    def test_a_mixed_list_answers_each_row_on_its_own_setting(self) -> None:
        """The shape a batching bug actually takes: one row's answer smeared
        across its neighbours."""
        friend = _profile(contact_visibility=VisibilityChoice.FRIENDS)
        self._befriend(friend)
        stranger = _profile(contact_visibility=VisibilityChoice.FRIENDS)
        public = _profile(contact_visibility=VisibilityChoice.ANYONE)
        silent = _profile(contact_visibility=VisibilityChoice.NO_ONE)

        self._assert_agrees([friend, stranger, public, silent, self.viewer])

    def test_the_two_fields_are_resolved_independently(self) -> None:
        """One profile, opposite settings on the two fields. A helper that read the
        wrong attribute would answer both alike and pass every test above."""
        subject = _profile(
            profile_visibility=VisibilityChoice.ANYONE,
            contact_visibility=VisibilityChoice.NO_ONE,
        )

        self.assertIn(subject.pk, Profile.visible_profile_pks(self.viewer, [subject]))
        self.assertNotIn(subject.pk, Profile.visible_contact_info_pks(self.viewer, [subject]))


class RelatedProfileIdsSupersetTests(TestCase):
    """``related_profile_ids`` narrows a queryset before the real check runs.

    It is allowed to be loose and is not allowed to be tight: an extra id costs
    one row for the real check to reject, while a missing one hides a profile
    the viewer is entitled to see. So every test here asserts membership, and
    none asserts absence.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()

    def _befriend(self, other: Profile) -> None:
        Friendship.objects.create(
            from_profile=self.viewer,
            to_profile=other,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )

    def test_the_viewer_is_related_to_themselves(self) -> None:
        self.assertIn(self.viewer.pk, Profile.related_profile_ids(self.viewer))

    def test_an_accepted_friend_is_included(self) -> None:
        friend = _profile()
        self._befriend(friend)

        self.assertIn(friend.pk, Profile.related_profile_ids(self.viewer))

    def test_a_friend_who_sent_the_request_is_included(self) -> None:
        other = _profile()
        Friendship.objects.create(
            from_profile=other,
            to_profile=self.viewer,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )

        self.assertIn(other.pk, Profile.related_profile_ids(self.viewer))

    def test_a_profile_with_a_pending_request_to_the_viewer_is_included(self) -> None:
        asker = _profile()
        Friendship.objects.create(
            from_profile=asker,
            to_profile=self.viewer,
            status=FriendshipStatus.REQUESTED,
            relationship_type=FriendshipType.FRIEND,
        )

        self.assertIn(asker.pk, Profile.related_profile_ids(self.viewer))

    def test_a_common_pin_partner_is_included(self) -> None:
        shared = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared, parent_pin=None)
        partner = _profile()
        baker.make(Pin, profile=partner, location=shared, parent_pin=None)

        self.assertIn(partner.pk, Profile.related_profile_ids(self.viewer))

    def test_a_common_pin_partner_on_the_same_place_is_included(self) -> None:
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        baker.make(Pin, profile=self.viewer, location=baker.make(Location, place=place), parent_pin=None)
        partner = _profile()
        baker.make(Pin, profile=partner, location=baker.make(Location, place=place), parent_pin=None)

        self.assertIn(partner.pk, Profile.related_profile_ids(self.viewer))

    def test_a_common_friend_partner_is_included(self) -> None:
        mutual = _profile()
        self._befriend(mutual)
        partner = _profile()
        Friendship.objects.create(
            from_profile=partner,
            to_profile=mutual,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )

        self.assertIn(partner.pk, Profile.related_profile_ids(self.viewer))

    def test_a_common_trip_partner_is_included(self) -> None:
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer)
        partner = _profile()
        TripMembership.objects.create(trip=trip, profile=partner)

        self.assertIn(partner.pk, Profile.related_profile_ids(self.viewer))

    def test_a_temporary_grant_holder_is_included(self) -> None:
        granted = _profile()
        DirectMessageTemporaryAccess.objects.create(
            profile=granted,
            granted_to=self.viewer,
            expires_at=timezone.now() + datetime.timedelta(days=1),
        )

        self.assertIn(granted.pk, Profile.related_profile_ids(self.viewer))

    def test_it_covers_everything_the_real_check_passes(self) -> None:
        """The contract, stated directly: for a population wired up every way a
        gate can be satisfied, no profile the per-row check admits is missing."""
        shared = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=shared, parent_pin=None)
        mutual = _profile()
        self._befriend(mutual)
        trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=trip, profile=self.viewer)

        population = [self.viewer]
        for value in VisibilityChoice.values:
            friend = _profile(profile_visibility=value, contact_visibility=value)
            self._befriend(friend)
            by_pin = _profile(profile_visibility=value, contact_visibility=value)
            baker.make(Pin, profile=by_pin, location=shared, parent_pin=None)
            by_friend = _profile(profile_visibility=value, contact_visibility=value)
            Friendship.objects.create(
                from_profile=by_friend,
                to_profile=mutual,
                status=FriendshipStatus.ACCEPTED,
                relationship_type=FriendshipType.FRIEND,
            )
            by_trip = _profile(profile_visibility=value, contact_visibility=value)
            TripMembership.objects.create(trip=trip, profile=by_trip)
            granted = _profile(profile_visibility=value, contact_visibility=value)
            DirectMessageTemporaryAccess.objects.create(
                profile=granted,
                granted_to=self.viewer,
                expires_at=timezone.now() + datetime.timedelta(days=1),
            )
            asker = _profile(profile_visibility=value, contact_visibility=value)
            Friendship.objects.create(
                from_profile=asker,
                to_profile=self.viewer,
                status=FriendshipStatus.REQUESTED,
                relationship_type=FriendshipType.FRIEND,
            )
            population += [friend, by_pin, by_friend, by_trip, granted, asker]

        related = Profile.related_profile_ids(self.viewer)
        missed = [
            subject
            for subject in population
            if subject.pk not in related
            and (subject.can_view_profile(self.viewer) or subject.can_view_contact_info(self.viewer))
        ]

        self.assertEqual(
            missed,
            [],
            "related_profile_ids must not miss a profile the per-row check passes",
        )
