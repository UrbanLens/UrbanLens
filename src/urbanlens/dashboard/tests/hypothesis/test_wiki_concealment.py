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

        composite = WikiStatVote.objects.composite(self.wiki, WikiStatField.VULNERABILITY, viewer_conceals=True, viewer=self.viewer)

        self.assertIsNone(composite.rounded)
        self.assertIsNone(composite.exact)
        self.assertEqual(composite.count, 0)

    def test_a_concealed_composite_still_reflects_the_viewers_own_vote(self) -> None:
        """The subtle half, and the one the branch was written for.

        Returning a flatly empty composite is its own tell: the page still
        renders "Your vote" from my_vote, so a concealed viewer who votes would
        see their stars filled beside a Community row that stays empty forever.
        A fresh wiki does not behave that way - there the sole voter's value
        *is* the composite - so one vote would be a reliable discriminator.

        Without passing `viewer`, the branch exits on its None guard and this
        goes untested, which is what the previous version of this test did.
        """
        from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField, WikiStatVote

        for _ in range(4):
            WikiStatVote.objects.cast(self.wiki, baker.make(User).profile, WikiStatField.VULNERABILITY, 5)
        WikiStatVote.objects.cast(self.wiki, self.viewer, WikiStatField.VULNERABILITY, 2)

        composite = WikiStatVote.objects.composite(self.wiki, WikiStatField.VULNERABILITY, viewer_conceals=True, viewer=self.viewer)

        self.assertEqual(composite.rounded, 2, "the viewer's own vote, not the community's 5s")
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


class RelatedRowConcealmentTests(TestCase):
    """Rows, as opposed to fields: comments, photos, aliases, links."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        self.viewer = baker.make(User).profile
        self.friend = baker.make(User).profile
        self.stranger = baker.make(User).profile
        baker.make(Friendship, from_profile=self.viewer, to_profile=self.friend, status=FriendshipStatus.ACCEPTED)

    def test_comments_keep_yours_and_your_friends_and_drop_strangers(self) -> None:
        """The general rule: a row with an actor is a contribution."""
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        mine = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.viewer)
        theirs = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.friend)
        others = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.stranger)

        visible = conceal_rows(Comment.objects.filter(wiki=self.wiki), self.viewer)

        self.assertIn(mine, visible)
        self.assertIn(theirs, visible)
        self.assertNotIn(others, visible)

    def test_provider_photos_stay_and_strangers_uploads_go(self) -> None:
        """Image.profile is the up-voter on a materialised provider row.

        So authorship only means anything when source is UPLOAD - reading the
        actor column alone would drop provider media a fresh wiki would show,
        and credit a voter for somebody else's photograph.
        """
        from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        provider = baker.make(
            Image,
            wiki=self.wiki,
            profile=self.stranger,
            source=ImageSource.WIKIMEDIA,
            media_type=MediaKind.PHOTO,
            image="pin_images/p.png",
        )
        stranger_upload = baker.make(
            Image,
            wiki=self.wiki,
            profile=self.stranger,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/s.png",
        )
        friend_upload = baker.make(
            Image,
            wiki=self.wiki,
            profile=self.friend,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/f.png",
        )

        visible = conceal_rows(Image.objects.filter(wiki=self.wiki), self.viewer)

        self.assertIn(provider, visible)
        self.assertIn(friend_upload, visible)
        self.assertNotIn(stranger_upload, visible)

    def test_a_provider_alias_stays_and_a_strangers_goes(self) -> None:
        """Provenance is the alias's own source, not created_by."""
        from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        provider = baker.make(WikiAlias, wiki=self.wiki, source="google_places", created_by=None)
        community = baker.make(WikiAlias, wiki=self.wiki, source=AliasSource.USER, created_by=self.stranger)

        visible = conceal_rows(WikiAlias.objects.filter(wiki=self.wiki), self.viewer)

        self.assertIn(provider, visible)
        self.assertNotIn(community, visible)

    def test_a_rename_created_alias_is_concealed_despite_a_null_author(self) -> None:
        """The exact case created_by gets wrong, and why the spec rejected it.

        Wiki.save() auto-creates an alias on every rename with created_by unset,
        so filtering on the author would re-expose - as an alias row - the very
        name concealed as a field.
        """
        from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        from_rename = baker.make(WikiAlias, wiki=self.wiki, name="A Stranger's Name For It", source=AliasSource.USER, created_by=None)

        visible = conceal_rows(WikiAlias.objects.filter(wiki=self.wiki), self.viewer)

        self.assertNotIn(from_rename, visible)

    def test_an_unknown_model_returns_nothing_rather_than_everything(self) -> None:
        """Failing closed is the only safe default for a concealment filter.

        A model nobody has written a rule for must not quietly return every
        row - that is the "one call site at a time" failure this table exists
        to avoid, and it would fail silently and permissively.
        """
        from urbanlens.dashboard.models.trips.model import Trip
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        baker.make(Trip)
        self.assertEqual(Trip.objects.count(), 1, "fixture must exist, or the assertion below is vacuous")

        self.assertEqual(conceal_rows(Trip.objects.all(), self.viewer).count(), 0)
