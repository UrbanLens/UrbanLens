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

from unittest import mock

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


class AggregateConcealmentTests(TestCase):
    """Counts and composites, which leak even when the rows behind them do not."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        self.viewer = baker.make(User).profile

    def test_the_concealed_community_summary_never_reaches_the_fuzz_cache(self) -> None:
        """The shared fuzz is keyed on an id with no viewer in it.

        So a concealed viewer arriving *after* an ordinary one would be handed
        the value that viewer populated - silently, only under concurrency.
        The concealed path has to short-circuit before the function, which is
        why this asserts on the call rather than on the number.
        """
        from unittest import mock

        from urbanlens.dashboard.services.wiki.concealment import concealed_community_summary

        with mock.patch("urbanlens.dashboard.services.wiki.community_counts.approximate_pin_count") as fuzz:
            summary = concealed_community_summary()

        fuzz.assert_not_called()
        self.assertTrue(summary["pin_count_low"])
        self.assertIsNone(summary["pin_count_approx"])
        self.assertIsNone(summary["first_pinned"])

    def test_a_concealed_stat_composite_reads_as_never_voted_on(self) -> None:
        """A rated danger/vulnerability is proof people have surveyed the place."""
        from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField, WikiStatVote

        for _ in range(4):
            WikiStatVote.objects.cast(self.wiki, baker.make(User).profile, WikiStatField.VULNERABILITY, 5)

        composite = WikiStatVote.objects.composite(self.wiki, WikiStatField.VULNERABILITY, viewer_conceals=True)

        self.assertIsNone(composite.rounded)
        self.assertIsNone(composite.exact)
        self.assertEqual(composite.count, 0)

    def test_an_unconcealed_stat_composite_still_reports(self) -> None:
        """Positive control: deleting the aggregate would pass the test above."""
        from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField, WikiStatVote

        WikiStatVote.objects.cast(self.wiki, self.viewer, WikiStatField.VULNERABILITY, 4)

        composite = WikiStatVote.objects.composite(self.wiki, WikiStatField.VULNERABILITY)

        self.assertEqual(composite.rounded, 4)


class InvertedTellTests(TestCase):
    """Cases where concealing something is louder than not concealing it."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make(User).profile

    def test_the_boundary_dialog_still_auto_opens_for_a_concealed_viewer(self) -> None:
        """A place nobody has voted on opens this dialog on arrival.

        So a concealed viewer who does *not* get it has been told other people
        have been here - the concealment announcing itself by staying quiet.
        """
        from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
        from urbanlens.dashboard.services.geo.boundary_voting import boundary_vote_context

        place = baker.make("dashboard.Place")
        options = baker.make("dashboard.Boundary", place=place, _quantity=2)
        baker.make(BoundaryVote, place=place, boundary=options[0], profile=baker.make(User).profile)

        with mock.patch("urbanlens.dashboard.services.geo.boundary_voting.boundary_options", return_value=options):
            revealed = boundary_vote_context(place, self.viewer)
            concealed = boundary_vote_context(place, self.viewer, conceal=True)

        self.assertFalse(revealed["auto_open"], "other people have voted, so it stays behind the button")
        self.assertTrue(concealed["auto_open"], "concealed, so it must behave as though nobody has")

    def test_your_own_vote_does_not_suppress_the_dialog_elsewhere(self) -> None:
        """Predates concealment: has_votes counted your own row against you."""
        from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
        from urbanlens.dashboard.services.geo.boundary_voting import boundary_vote_context

        place = baker.make("dashboard.Place")
        options = baker.make("dashboard.Boundary", place=place, _quantity=2)
        baker.make(BoundaryVote, place=place, boundary=options[0], profile=self.viewer)

        with mock.patch("urbanlens.dashboard.services.geo.boundary_voting.boundary_options", return_value=options):
            context = boundary_vote_context(place, self.viewer)

        self.assertTrue(context["auto_open"])
