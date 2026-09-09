"""``authorize_comment_image`` must apply every gate the comment thread itself applies.

A comment's image is served through a separate media-authorization path
(``services.media.access.authorize_comment_image``) rather than alongside the
comment text, so it has always needed its own copy of the visibility gates -
author ``comment_visibility``, a pending malware scan, host (pin/trip)
membership, and an ``@loc``/``@activity`` mention the viewer hasn't resolved.
The first three were covered; the mention gate was not, so a wiki or trip
comment `visible_comment_tree`/`build_comment_tree` drops entirely for naming
an unpinned location still served its own attached image.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.media.access import authorize_comment_image


class WikiCommentImageMentionGateTests(TestCase):
    """A wiki comment naming an unpinned location must not leak its image either."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = Profile.objects.get(user=baker.make(User))
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.viewer, location=self.location)  # earns wiki access

        self.author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()

        self.secret_location = baker.make(Location)

    def _comment(self, text: str) -> Comment:
        return baker.make(Comment, wiki=self.wiki, profile=self.author, text=text, image="comment_images/x.png")

    def test_comment_naming_an_unpinned_location_hides_its_image(self) -> None:
        comment = self._comment(f"See also @[Secret Spot](loc:{self.secret_location.uuid})")
        self.assertFalse(authorize_comment_image(self.viewer, comment.image.name))

    def test_an_ordinary_comment_still_shows_its_image(self) -> None:
        """Anti-vacuity: the gate must not blanket-deny every wiki comment image."""
        comment = self._comment("A perfectly ordinary comment")
        self.assertTrue(authorize_comment_image(self.viewer, comment.image.name))

    def test_the_authors_own_view_is_unaffected_by_the_mention_gate(self) -> None:
        comment = self._comment(f"See also @[Secret Spot](loc:{self.secret_location.uuid})")
        self.assertTrue(authorize_comment_image(self.author, comment.image.name))


class TripCommentImageMentionGateTests(TestCase):
    """The same gate, for a trip comment's ``@activity``/``@loc`` mentions."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.viewer = Profile.objects.get(user=baker.make(User))
        self.author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()

        self.trip = baker.make(Trip)
        TripMembership.objects.create(trip=self.trip, profile=self.viewer)
        TripMembership.objects.create(trip=self.trip, profile=self.author)

        self.secret_location = baker.make(Location)

    def _comment(self, text: str) -> TripComment:
        return baker.make(TripComment, trip=self.trip, author=self.author, text=text, image="comment_images/y.png")

    def test_comment_naming_an_unresolvable_location_hides_its_image(self) -> None:
        comment = self._comment(f"Detour via @[Secret Spot](loc:{self.secret_location.uuid})")
        self.assertFalse(authorize_comment_image(self.viewer, comment.image.name))

    def test_an_ordinary_trip_comment_still_shows_its_image(self) -> None:
        comment = self._comment("Great photo from the trip")
        self.assertTrue(authorize_comment_image(self.viewer, comment.image.name))
