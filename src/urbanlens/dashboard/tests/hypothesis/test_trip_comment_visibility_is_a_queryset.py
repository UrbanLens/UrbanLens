"""``TripComment.objects.visible_to`` must admit exactly what the tree builder admits.

The trip comment panel and the trip comment API both build the entire thread -
every comment, every reply, every reaction - and never paginate it at all,
because the three gates were answered in Python. That is H43 and H52, and it is
the same defect the pin/wiki comment endpoints had: a gate that cannot be
expressed as a queryset is a gate that stops anything above it from paging.

Expressing it as a queryset makes it a second implementation of a decision that
already had one, so it is held to ``trip_comment_is_visible`` across the product
of every ``VisibilityChoice`` and every relationship that can satisfy one.

Two things differ from the pin/wiki side and are tested directly, because a
shared implementation is exactly where a difference like this gets lost:

- ``TripComment.author`` is ``SET_NULL``, so a comment whose author is gone has
  no visibility preference left to enforce and stays visible.
- The author's ``comment_visibility`` is the governing field on both, so the
  gate is genuinely the same rule rather than a similar one.
"""

from __future__ import annotations

import uuid as uuid_module

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.services.trips.trip_comments import trip_comment_is_visible

from .test_comment_visibility_is_a_queryset import RELATIONSHIPS, _profile


class TripVisibleToAgreesWithThePerRowCheckTests(TestCase):
    """The queryset and the predicate name the same trip comments."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()
        self.trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)

        self.shared_place = baker.make(Place, kind=PlaceKind.PARCEL)
        baker.make(Pin, profile=self.viewer, location=baker.make(Location, place=self.shared_place), parent_pin=None)
        self.placeless_location = baker.make(Location, place=None)
        baker.make(Pin, profile=self.viewer, location=self.placeless_location, parent_pin=None)
        self.mutual_friend = _profile()
        self._accept(self.viewer, self.mutual_friend)
        self.mutual_friend_who_asked = _profile()
        self._accept(self.mutual_friend_who_asked, self.viewer)
        self.shared_trip = baker.make(Trip, creator=self.viewer)
        TripMembership.objects.create(trip=self.shared_trip, profile=self.viewer)

    def _accept(self, sender: Profile, receiver: Profile) -> None:
        Friendship.objects.create(
            from_profile=sender,
            to_profile=receiver,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )

    def _relate(self, author: Profile, relationship: str) -> None:
        if relationship == "friend":
            self._accept(self.viewer, author)
        elif relationship == "friend_sent_by_author":
            self._accept(author, self.viewer)
        elif relationship == "pending_request":
            Friendship.objects.create(
                from_profile=author,
                to_profile=self.viewer,
                status=FriendshipStatus.REQUESTED,
                relationship_type=FriendshipType.FRIEND,
            )
        elif relationship == "common_pin":
            baker.make(Pin, profile=author, location=baker.make(Location, place=self.shared_place), parent_pin=None)
        elif relationship == "common_pin_placeless":
            baker.make(Pin, profile=author, location=self.placeless_location, parent_pin=None)
        elif relationship == "common_friend":
            self._accept(author, self.mutual_friend)
        elif relationship == "common_friend_author_received":
            self._accept(self.mutual_friend, author)
        elif relationship == "common_friend_viewer_received":
            self._accept(author, self.mutual_friend_who_asked)
        elif relationship == "common_friend_both_received":
            self._accept(self.mutual_friend_who_asked, author)
        elif relationship == "common_trip":
            TripMembership.objects.create(trip=self.shared_trip, profile=author)

    def _assert_agrees(self, comments: list[TripComment]) -> None:
        admitted = set(TripComment.objects.visible_to(self.viewer).values_list("pk", flat=True))
        assert_agrees(
            lambda comment: trip_comment_is_visible(comment, self.viewer),
            lambda comment: comment.pk in admitted,
            comments,
            describe=lambda comment: f"{comment.text} (author {comment.author_id})",
            label="TripComment.objects.visible_to",
        )

    def _author_matrix(self) -> list[TripComment]:
        comments = []
        for visibility in VisibilityChoice.values:
            for relationship in RELATIONSHIPS:
                author = _profile(comment_visibility=visibility)
                self._relate(author, relationship)
                comments.append(
                    TripComment.objects.create(trip=self.trip, author=author, text=f"{visibility}/{relationship}")
                )
        return comments

    def test_every_setting_against_every_relationship(self) -> None:
        self._assert_agrees(self._author_matrix())

    def test_the_matrix_covers_both_answers(self) -> None:
        comments = self._author_matrix()
        decisions = {trip_comment_is_visible(comment, self.viewer) for comment in comments}
        self.assertEqual(
            decisions, {True, False}, "The matrix must contain comments the gate hides and comments it admits."
        )

    def test_a_comment_whose_author_was_deleted_stays_visible(self) -> None:
        """SET_NULL, so there is no preference left to enforce - unlike the pin side."""
        orphan = TripComment.objects.create(trip=self.trip, author=None, text="left by a closed account")
        self._assert_agrees([orphan])

    def test_an_orphaned_comment_awaiting_its_scan_is_still_hidden(self) -> None:
        """Gate 2 does not care that the author is gone."""
        orphan = TripComment.objects.create(trip=self.trip, author=None, text="scanning", pending_scan=True)
        self._assert_agrees([orphan])

    def test_the_viewer_always_sees_their_own(self) -> None:
        Profile.objects.filter(pk=self.viewer.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        self.viewer.refresh_from_db()
        own = TripComment.objects.create(trip=self.trip, author=self.viewer, text="mine")
        scanning = TripComment.objects.create(
            trip=self.trip, author=self.viewer, text="mine, scanning", pending_scan=True
        )
        self._assert_agrees([own, scanning])

    def test_a_mention_the_viewer_has_not_pinned_hides_the_comment(self) -> None:
        author = _profile(comment_visibility=VisibilityChoice.ANYONE)
        unpinned = Location.objects.create(latitude=41.0, longitude=-73.0)
        comments = [
            TripComment.objects.create(trip=self.trip, author=author, text=f"see @[X](loc:{unpinned.uuid})"),
            TripComment.objects.create(
                trip=self.trip, author=author, text=f"see @[X](loc:{self.placeless_location.uuid})"
            ),
            TripComment.objects.create(trip=self.trip, author=author, text=f"see @[X](loc:{uuid_module.uuid4()})"),
        ]
        self._assert_agrees(comments)

    def test_an_unresolvable_activity_mention_does_not_hide_anything(self) -> None:
        """render_comment_text escapes an unknown @act token; it does not drop the row."""
        author = _profile(comment_visibility=VisibilityChoice.ANYONE)
        comment = TripComment.objects.create(trip=self.trip, author=author, text="meeting at @act:99 tomorrow")
        self.assertTrue(trip_comment_is_visible(comment, self.viewer))
        self._assert_agrees([comment])
