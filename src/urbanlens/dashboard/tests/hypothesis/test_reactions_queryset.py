"""Tests for ReactionQuerySet.existing().

Part of the ongoing "every model gets its own queryset/manager" cleanup -
Reaction was still on the bare default manager despite the exact same
"does this profile+emoji+target already exist" lookup being duplicated
across all four of its polymorphic targets (comment/trip_comment/
direct_message/group_message toggle views in controllers/comments.py,
services/direct_messages.py and services/group_chats.py).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupMessage
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


class ReactionExistingForGroupMessageTests(TestCase):
    """The group-message host, added last and therefore the most likely to drift.

    A group message is the only reaction target whose row is visible to more
    than two people, so the per-host unique constraint matters more here than
    anywhere else: without it, two concurrent taps from the same member would
    each insert a row and the aggregate count shown to the whole group would
    read 2 for one person.
    """

    def setUp(self) -> None:
        """Create a group with one member and one message in it."""
        self.user = baker.make(User)
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.group = GroupChat.objects.create(name="Weekend crew", creator=self.profile)
        self.message = GroupMessage.objects.create(group=self.group, sender=self.profile, body="anyone free saturday")

    def test_matches_by_group_message_target(self) -> None:
        reaction = Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)
        self.assertEqual(Reaction.objects.existing(self.profile, "🔥", group_message=self.message), reaction)

    def test_returns_none_when_no_reaction_exists(self) -> None:
        self.assertIsNone(Reaction.objects.existing(self.profile, "🔥", group_message=self.message))

    def test_does_not_match_the_same_emoji_on_a_different_group_message(self) -> None:
        other_message = GroupMessage.objects.create(group=self.group, sender=self.profile, body="never mind")
        Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=other_message)
        self.assertIsNone(Reaction.objects.existing(self.profile, "🔥", group_message=self.message))

    def test_related_name_reaches_back_from_the_message(self) -> None:
        """``message.reactions`` is what the thread renderer prefetches."""
        reaction = Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)
        self.assertEqual(list(self.message.reactions.all()), [reaction])

    def test_duplicate_reaction_is_refused_by_the_partial_unique_constraint(self) -> None:
        Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)

    def test_different_emoji_on_the_same_message_is_allowed(self) -> None:
        Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)
        Reaction.objects.create(profile=self.profile, emoji="👍", group_message=self.message)
        self.assertEqual(self.message.reactions.count(), 2)

    def test_two_members_may_use_the_same_emoji(self) -> None:
        """The constraint is per (profile, emoji, host) - not per (emoji, host)."""
        other_profile, _ = Profile.objects.get_or_create(user=baker.make(User))
        Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)
        Reaction.objects.create(profile=other_profile, emoji="🔥", group_message=self.message)
        self.assertEqual(self.message.reactions.count(), 2)

    def test_deleting_the_message_cascades_to_its_reactions(self) -> None:
        """A hard-deleted message must not leave orphan reaction rows behind."""
        Reaction.objects.create(profile=self.profile, emoji="🔥", group_message=self.message)
        self.message.delete()
        self.assertFalse(Reaction.objects.filter(emoji="🔥").exists())


class ReactionExistingTargetGuardTests(TestCase):
    """``existing()`` must refuse anything but exactly one host kwarg.

    Both malformed calls fail *silently* without the guard, which is why it
    exists. Two hosts AND together into a filter no row can satisfy (no row
    has two hosts set), so the answer is always "no existing reaction" and the
    toggle inserts a duplicate the database then rejects with an opaque
    IntegrityError. Zero hosts matches any reaction by that profile with that
    emoji *anywhere on the site*, so the toggle removes an unrelated one.
    """

    def setUp(self) -> None:
        """Create a profile with one comment and one group message to react to."""
        self.user = baker.make(User)
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.comment = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile)
        self.group = GroupChat.objects.create(name="Weekend crew", creator=self.profile)
        self.message = GroupMessage.objects.create(group=self.group, sender=self.profile, body="hello")

    def test_two_hosts_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Reaction.objects.existing(self.profile, "🔥", comment=self.comment, group_message=self.message)

    def test_no_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Reaction.objects.existing(self.profile, "🔥")

    def test_unknown_kwarg_is_rejected(self) -> None:
        """A typo'd host name must not fall through to an opaque FieldError."""
        with self.assertRaises(ValueError):
            Reaction.objects.existing(self.profile, "🔥", groupmessage=self.message)
