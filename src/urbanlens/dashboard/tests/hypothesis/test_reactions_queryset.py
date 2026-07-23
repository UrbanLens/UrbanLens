"""Tests for ReactionQuerySet.existing().

Part of the ongoing "every model gets its own queryset/manager" cleanup -
Reaction was still on the bare default manager despite the exact same
"does this profile+emoji+target already exist" lookup being duplicated
across all three of its polymorphic targets (comment/trip_comment/
direct_message toggle views in controllers/comments.py and
services/direct_messages.py).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.models.trips.model import Trip, TripComment


class ReactionExistingForCommentTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.comment = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile)

    def test_returns_the_matching_reaction(self) -> None:
        reaction = Reaction.objects.create(profile=self.profile, emoji="👍", comment=self.comment)
        self.assertEqual(Reaction.objects.existing(self.profile, "👍", comment=self.comment), reaction)

    def test_returns_none_when_no_reaction_exists(self) -> None:
        self.assertIsNone(Reaction.objects.existing(self.profile, "👍", comment=self.comment))

    def test_does_not_match_a_different_emoji(self) -> None:
        Reaction.objects.create(profile=self.profile, emoji="👍", comment=self.comment)
        self.assertIsNone(Reaction.objects.existing(self.profile, "🔥", comment=self.comment))

    def test_does_not_match_a_different_profiles_reaction(self) -> None:
        other_profile, _ = Profile.objects.get_or_create(user=baker.make(User))
        Reaction.objects.create(profile=other_profile, emoji="👍", comment=self.comment)
        self.assertIsNone(Reaction.objects.existing(self.profile, "👍", comment=self.comment))

    def test_does_not_match_the_same_emoji_on_a_different_comment(self) -> None:
        other_comment = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile)
        Reaction.objects.create(profile=self.profile, emoji="👍", comment=other_comment)
        self.assertIsNone(Reaction.objects.existing(self.profile, "👍", comment=self.comment))


class ReactionExistingForTripCommentAndDirectMessageTests(TestCase):
    """Same lookup, exercised against the other two polymorphic target kinds."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

    def test_matches_by_trip_comment_target(self) -> None:
        trip = Trip.objects.create(name="Loop trail", creator=self.profile)
        trip_comment = baker.make(TripComment, trip=trip, author=self.profile)
        reaction = Reaction.objects.create(profile=self.profile, emoji="🎉", trip_comment=trip_comment)
        self.assertEqual(Reaction.objects.existing(self.profile, "🎉", trip_comment=trip_comment), reaction)

    def test_matches_by_direct_message_target(self) -> None:
        other_user = baker.make(User)
        other_profile, _ = Profile.objects.get_or_create(user=other_user)
        message = DirectMessage.objects.create(sender=other_profile, recipient=self.profile, body="hi")
        reaction = Reaction.objects.create(profile=self.profile, emoji="❤", direct_message=message)
        self.assertEqual(Reaction.objects.existing(self.profile, "❤", direct_message=message), reaction)

    def test_a_trip_comment_reaction_does_not_match_a_direct_message_lookup(self) -> None:
        """Sanity check the kwarg is genuinely target-discriminating, not just profile+emoji."""
        trip = Trip.objects.create(name="Ridge walk", creator=self.profile)
        trip_comment = baker.make(TripComment, trip=trip, author=self.profile)
        Reaction.objects.create(profile=self.profile, emoji="🎉", trip_comment=trip_comment)

        other_user = baker.make(User)
        other_profile, _ = Profile.objects.get_or_create(user=other_user)
        message = DirectMessage.objects.create(sender=other_profile, recipient=self.profile, body="hi")
        self.assertIsNone(Reaction.objects.existing(self.profile, "🎉", direct_message=message))
