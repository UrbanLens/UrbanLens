"""``Comment.objects.visible_to`` must admit exactly what ``comment_is_visible`` admits.

The comment endpoints built a whole thread - every reply, reaction, markup map
and a per-author permission check - and paginated the built list, because the
three visibility gates were answered in Python and so the rows a page should
contain were not knowable in SQL. Expressing the gates as a queryset is what
lets the database do the paging, and it is a second implementation of a
decision that already had one.

That is the failure mode this file exists for. A batch or SQL form of a
visibility rule drifts silently, because each side is self-consistent; nothing
breaks when they disagree, someone is just shown a comment they should not
have been. So the filter is held to ``comment_is_visible`` across the product
of every ``VisibilityChoice`` and every relationship that can satisfy one,
rather than against expectations written by the same author who wrote the
filter.

Gate 4 (identity masking) is deliberately absent here: it shapes how a
surviving author is displayed, not whether a row is admitted, and stays in
Python where the page is rendered.
"""

from __future__ import annotations

import uuid as uuid_module

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.comments.comments import comment_is_visible

#: Every way a non-ANYONE gate can be satisfied, plus the case where none is.
RELATIONSHIPS = (
    "none",
    "friend",
    "friend_sent_by_author",
    "pending_request",
    "common_pin",
    "common_friend",
    "common_trip",
)


def _profile(**kwargs) -> Profile:
    profile = baker.make(User).profile
    if kwargs:
        Profile.objects.filter(pk=profile.pk).update(**kwargs)
        profile.refresh_from_db()
    return profile


class VisibleToAgreesWithThePerRowCheckTests(TestCase):
    """The queryset and the predicate name the same comments."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()
        self.host = baker.make(Pin, profile=self.viewer, location=baker.make(Location), parent_pin=None)

        # Relationship scaffolding the authors below attach themselves to.
        self.shared_place = baker.make(Place, kind=PlaceKind.PARCEL)
        baker.make(Pin, profile=self.viewer, location=baker.make(Location, place=self.shared_place), parent_pin=None)
        self.mutual_friend = _profile()
        self._accept(self.viewer, self.mutual_friend)
        # The same mutual, reached by a row pointing the other way, so an author
        # can be related through either direction at the viewer's end.
        self.mutual_friend_who_asked = _profile()
        self._accept(self.mutual_friend_who_asked, self.viewer)
        # A Location no provider resolved to a Place - the fallback half of the
        # common-pin key.
        self.placeless_location = baker.make(Location, place=None)
        baker.make(Pin, profile=self.viewer, location=self.placeless_location, parent_pin=None)
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

    def _author_matrix(self) -> list[Comment]:
        """One author, and one comment, per (setting, relationship) pair."""
        comments = []
        for visibility in VisibilityChoice.values:
            for relationship in RELATIONSHIPS:
                author = _profile(comment_visibility=visibility)
                self._relate(author, relationship)
                comments.append(
                    Comment.objects.create(pin=self.host, profile=author, text=f"{visibility}/{relationship}")
                )
        return comments

    def _assert_agrees(self, comments: list[Comment]) -> None:
        admitted = set(Comment.objects.visible_to(self.viewer).values_list("pk", flat=True))
        assert_agrees(
            lambda comment: comment_is_visible(comment, self.viewer),
            lambda comment: comment.pk in admitted,
            comments,
            describe=lambda comment: f"{comment.text} (author {comment.profile_id})",
            label="Comment.objects.visible_to",
        )

    def test_every_setting_against_every_relationship(self) -> None:
        self._assert_agrees(self._author_matrix())

    def test_the_matrix_covers_both_answers(self) -> None:
        """A filter admitting everything, or nothing, would pass a one-sided matrix."""
        comments = self._author_matrix()
        decisions = {comment_is_visible(comment, self.viewer) for comment in comments}
        self.assertEqual(
            decisions, {True, False}, "The matrix must contain comments the gate hides and comments it admits."
        )

    def test_a_viewer_always_sees_their_own_comment(self) -> None:
        """Even under NO_ONE - can_view_comments_from short-circuits on self."""
        Profile.objects.filter(pk=self.viewer.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        self.viewer.refresh_from_db()
        own = Comment.objects.create(pin=self.host, profile=self.viewer, text="mine")
        self._assert_agrees([own])

    def test_an_unscanned_image_hides_the_comment_from_everyone_but_its_author(self) -> None:
        """Gate 2, which has nothing to do with the author's settings."""
        author = _profile(comment_visibility=VisibilityChoice.ANYONE)
        theirs = Comment.objects.create(pin=self.host, profile=author, text="scanning", pending_scan=True)
        mine = Comment.objects.create(pin=self.host, profile=self.viewer, text="mine, scanning", pending_scan=True)
        self._assert_agrees([theirs, mine])

    def test_a_mention_the_viewer_has_not_pinned_still_hides_the_comment(self) -> None:
        """Gate 3, composed with the other two rather than replacing them."""
        author = _profile(comment_visibility=VisibilityChoice.ANYONE)
        unpinned = Location.objects.create(latitude=41.0, longitude=-73.0)
        comments = [
            Comment.objects.create(pin=self.host, profile=author, text=f"see @[X](loc:{unpinned.uuid})"),
            Comment.objects.create(pin=self.host, profile=author, text=f"see @[X](loc:{self.host.location.uuid})"),
            Comment.objects.create(pin=self.host, profile=author, text=f"see @[X](loc:{uuid_module.uuid4()})"),
        ]
        self._assert_agrees(comments)

    def test_the_three_gates_compose(self) -> None:
        """A comment failing any one gate is out, whatever the other two say."""
        author = _profile(comment_visibility=VisibilityChoice.NO_ONE)
        self._accept(self.viewer, author)
        unpinned = Location.objects.create(latitude=42.0, longitude=-72.0)
        comments = [
            Comment.objects.create(pin=self.host, profile=author, text="friend of mine, but NO_ONE"),
            Comment.objects.create(pin=self.host, profile=author, text=f"and @[X](loc:{unpinned.uuid})"),
            Comment.objects.create(pin=self.host, profile=author, text="and scanning", pending_scan=True),
        ]
        self._assert_agrees(comments)
