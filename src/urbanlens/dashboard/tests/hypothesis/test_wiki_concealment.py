"""What a concealed wiki shows: automatic writes, your own, and your friends'.

The friend clause is the one that shapes the design, and the one worth testing
hardest. Friends talk offline - "just check the wiki, I put a load of stuff up
there" - so a concealed page that hides a friend's contribution does not just
conceal a place, it makes the site look broken to somebody who was told what to
expect.

The security-indicator rule is the other ruling with teeth: those are unset
whatever their provenance, including when a provider supplied them, because a
place that reads as surveyed is exactly what concealment exists to prevent.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision
from urbanlens.dashboard.services.wiki.concealment import accepted_friend_ids, concealed_field_values


class ConcealedValueTests(TestCase):
    """Resolving the field set a concealed viewer is entitled to."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        WikiFieldRevision.objects.filter(target=self.wiki).delete()

        self.viewer = baker.make(User).profile
        self.friend = baker.make(User).profile
        self.stranger = baker.make(User).profile
        baker.make(Friendship, from_profile=self.viewer, to_profile=self.friend, status=FriendshipStatus.ACCEPTED)

        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Provider Name")

    def test_automatic_content_is_shown(self) -> None:
        """A brand-new wiki carries enrichment data, so a concealed one must too."""
        values = concealed_field_values(self.wiki, self.viewer)

        self.assertEqual(values["name"], "Provider Name")

    def test_a_strangers_contribution_is_hidden(self) -> None:
        """The thing the whole feature exists for."""
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="A stranger's notes on getting in")

        values = concealed_field_values(self.wiki, self.viewer)

        self.assertNotIn("getting in", str(values["description"]))

    def test_a_friends_contribution_is_shown(self) -> None:
        """Friends talk offline; the page must not contradict them."""
        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="I put this here")

        values = concealed_field_values(self.wiki, self.viewer)

        self.assertEqual(values["description"], "I put this here")

    def test_your_own_contribution_is_shown(self) -> None:
        """The own-content rule holds here as everywhere else."""
        with writing_as(WriteSource.USER, actor=self.viewer.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="My own note")

        values = concealed_field_values(self.wiki, self.viewer)

        self.assertEqual(values["description"], "My own note")

    def test_a_strangers_later_edit_does_not_displace_a_friends(self) -> None:
        """Ordering runs over the qualifying rows, not all of them."""
        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="Friend's version")
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="Stranger's version")

        values = concealed_field_values(self.wiki, self.viewer)

        self.assertEqual(values["description"], "Friend's version")

    def test_security_indicators_are_unset_even_when_a_provider_set_them(self) -> None:
        """The one rule that ignores provenance entirely.

        A place reading as surveyed - fences, cameras, alarms - is precisely
        what concealment exists to prevent, so the source does not matter.
        """
        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(cameras=SecurityLevel.SOME, fences=SecurityLevel.SOME)

        values = concealed_field_values(self.wiki, self.viewer)

        self.assertNotEqual(values["cameras"], SecurityLevel.SOME)
        self.assertNotEqual(values["fences"], SecurityLevel.SOME)

    def test_a_signed_out_viewer_sees_only_automatic_content(self) -> None:
        """No profile means no friends and no own content - not a crash."""
        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="Friend's note")

        values = concealed_field_values(self.wiki, None)

        self.assertEqual(values["name"], "Provider Name")
        self.assertNotEqual(values["description"], "Friend's note")


class FriendResolutionTests(TestCase):
    """Who counts as a friend for this purpose."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make(User).profile
        self.other = baker.make(User).profile

    def test_friendship_counts_in_either_direction(self) -> None:
        """Which side sent the request says nothing about the relationship now."""
        baker.make(Friendship, from_profile=self.other, to_profile=self.viewer, status=FriendshipStatus.ACCEPTED)

        self.assertIn(self.other.pk, accepted_friend_ids(self.viewer))

    def test_a_pending_request_is_not_a_friendship(self) -> None:
        """Otherwise sending a request to a stranger would unlock their content."""
        baker.make(Friendship, from_profile=self.viewer, to_profile=self.other, status=FriendshipStatus.PENDING)

        self.assertNotIn(self.other.pk, accepted_friend_ids(self.viewer))

    def test_a_muted_friend_is_still_a_friend(self) -> None:
        """Mute is a notification-volume control, orthogonal to the relationship."""
        baker.make(
            Friendship,
            from_profile=self.viewer,
            to_profile=self.other,
            status=FriendshipStatus.ACCEPTED,
            muted_by_from_profile=True,
        )

        self.assertIn(self.other.pk, accepted_friend_ids(self.viewer))
