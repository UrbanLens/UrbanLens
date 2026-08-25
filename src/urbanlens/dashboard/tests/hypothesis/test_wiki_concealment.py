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

import datetime
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.article.model import EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, Article, ArticleRevision
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision
from urbanlens.dashboard.services.wiki.concealment import accepted_friend_ids, conceal_article, conceal_wiki, concealed_field_values, is_concealed, visible_rows, writable_wiki


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


class ProjectionTests(TestCase):
    """The object ``conceal_wiki`` hands back, and what may be done with it.

    The rework these cover replaced a ``__getattr__`` proxy, which failed *open*:
    anything it did not explicitly override - far more than what it did - fell
    through to the real row. A projection is a real ``Wiki`` instead, so the
    failure mode inverts. These pin the properties that inversion depends on.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        WikiFieldRevision.objects.filter(target=self.wiki).delete()

        self.viewer = baker.make(User).profile
        self.other = baker.make(User).profile
        self.stranger = baker.make(User).profile

        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="A stranger's notes", cameras=SecurityLevel.EVERYWHERE)
        self.wiki.refresh_from_db()

    def _concealed(self, viewer=None):
        """Conceal ``self.wiki`` with the gate forced on."""
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            return conceal_wiki(self.wiki, viewer if viewer is not None else self.viewer)

    def test_a_projection_is_a_real_wiki_with_the_real_primary_key(self) -> None:
        """Templates, serializers and foreign keys all depend on this.

        The proxy this replaced could not be assigned to a ``ForeignKey`` at
        all, which is why every write path had to be taught it existed.
        """
        projection = self._concealed()

        self.assertIsInstance(projection, Wiki)
        self.assertEqual(projection.pk, self.wiki.pk)

    def test_a_projection_carries_concealed_values(self) -> None:
        """The point of the object."""
        projection = self._concealed()

        self.assertNotIn("stranger", str(projection.description or ""))

    def test_concealing_does_not_disturb_the_row_it_came_from(self) -> None:
        """A shallow copy shares state with its original unless prevented."""
        real_description = self.wiki.description

        self._concealed()

        self.assertEqual(self.wiki.description, real_description)
        self.assertEqual(Wiki.objects.get(pk=self.wiki.pk).description, real_description)

    def test_the_display_helper_reflects_the_concealed_value(self) -> None:
        """``get_cameras_display()`` reads the field, so it must read the concealed one.

        The proxy needed explicit code for this and the security chips render
        through exactly this call - if it regressed, a concealed page would show
        a stranger's survey in words while the field said otherwise.
        """
        projection = self._concealed()
        unset = Wiki(cameras=Wiki._meta.get_field("cameras").get_default())

        self.assertEqual(self.wiki.get_cameras_display(), SecurityLevel.EVERYWHERE.label)
        self.assertEqual(projection.get_cameras_display(), unset.get_cameras_display())

    def test_saving_a_projection_is_refused(self) -> None:
        """The one thing that must never happen: concealment persisted over content."""
        projection = self._concealed()

        with self.assertRaises(TypeError):
            projection.save()

    def test_deleting_a_projection_is_refused(self) -> None:
        """Same reason as saving."""
        projection = self._concealed()

        with self.assertRaises(TypeError):
            projection.delete()

    def test_writable_wiki_returns_a_row_carrying_the_real_values(self) -> None:
        """What every write path downstream of the read gate has to do."""
        projection = self._concealed()

        target = writable_wiki(projection)

        self.assertFalse(is_concealed(target))
        self.assertEqual(target.description, self.wiki.description)
        self.assertEqual(target.pk, self.wiki.pk)

    def test_writable_wiki_leaves_an_ordinary_row_alone(self) -> None:
        """No extra query on the path everybody is on."""
        self.assertIs(writable_wiki(self.wiki), self.wiki)

    def test_concealing_twice_for_one_viewer_reuses_the_projection(self) -> None:
        """Several surfaces conceal a wiki the resolve gate already concealed."""
        projection = self._concealed()

        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            again = conceal_wiki(projection, self.viewer)

        self.assertIs(again, projection)

    def test_a_property_computed_from_concealed_fields_reads_the_concealed_ones(self) -> None:
        """The failure the projection exists to make impossible.

        ``effective_date_last_active`` derives from two versioned fields. The
        proxy this replaced answered it by delegating to the real row, so the
        property computed from the *stored* dates and handed back a stranger's
        answer through a concealed object - the fields were hidden and the
        conclusion drawn from them was not. A projection is a real ``Wiki``, so
        the property runs against the values this viewer is entitled to.
        """
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(date_last_active=datetime.date(2019, 6, 1))
        self.wiki.refresh_from_db()

        projection = self._concealed()

        self.assertEqual(self.wiki.effective_date_last_active, datetime.date(2019, 6, 1))
        self.assertIsNone(projection.effective_date_last_active)

    def test_an_ungated_viewer_never_inherits_someone_elses_projection(self) -> None:
        """The fail-open the viewer key exists to close.

        Reuse is an optimisation; if the second viewer is not gated at all, the
        cheap answer would be to hand the projection straight back, which serves
        one person's redacted view to somebody entitled to the whole row.
        """
        projection = self._concealed()

        # Gate off for this second viewer - the ordinary, ungated path.
        shown = conceal_wiki(projection, self.other)

        self.assertFalse(is_concealed(shown))
        self.assertEqual(shown.description, self.wiki.description)

    def test_concealing_someone_elses_projection_rebuilds_it(self) -> None:
        """Reuse is keyed on the viewer, or one person's view could be served to another."""
        projection = self._concealed()

        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            for_other = conceal_wiki(projection, self.other)

        self.assertIsNot(for_other, projection)


class ConcealedArticleTests(TestCase):
    """What a concealed viewer reads on the Article tab.

    An article is prose, not fields, so there is nothing to resolve write-by-
    write. What makes it tractable is that every ``ArticleRevision`` stores the
    complete source as of that revision: showing the newest revision a viewer
    may see needs no reconstruction.

    The rule with teeth is the null editor. It means a Wikipedia seed *or* a
    deleted account, and treating the two alike would hand a stranger's prose to
    a concealed viewer on the strength of that stranger having closed their
    account.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        self.viewer = baker.make(User).profile
        self.friend = baker.make(User).profile
        self.stranger = baker.make(User).profile
        baker.make(Friendship, from_profile=self.viewer, to_profile=self.friend, status=FriendshipStatus.ACCEPTED)

        self.article = baker.make(Article, wiki=self.wiki, pin=None, content="stranger text")

    def _revision(self, content: str, *, editor=None, summary: str = "") -> ArticleRevision:
        return baker.make(ArticleRevision, article=self.article, content=content, editor=editor, edit_summary=summary)

    def _shown(self):
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            return conceal_article(self.article, self.wiki, self.viewer)

    def test_a_place_only_strangers_have_written_up_has_no_article(self) -> None:
        """Which is exactly what a place nobody has written up looks like."""
        self._revision("A stranger's write-up", editor=self.stranger)

        self.assertIsNone(self._shown())

    def test_a_wikipedia_seed_is_shown(self) -> None:
        """Automatic content is public information the site relays; a fresh wiki carries it."""
        self._revision("Seeded prose", editor=None, summary=EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA)

        self.assertEqual(self._shown().content, "Seeded prose")

    def test_a_deleted_strangers_revision_is_not_mistaken_for_a_seed(self) -> None:
        """The trap: `editor` is null for a deleted account too."""
        self._revision("A deleted stranger's write-up", editor=None, summary="tidied up")

        self.assertIsNone(self._shown())

    def test_a_friends_revision_is_shown(self) -> None:
        """Friends talk offline - "I put a load of stuff on the wiki" has to work."""
        self._revision("Seeded prose", editor=None, summary=EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA)
        self._revision("My friend's write-up", editor=self.friend)

        self.assertEqual(self._shown().content, "My friend's write-up")

    def test_a_strangers_later_revision_does_not_displace_a_friends(self) -> None:
        """The newest *visible* revision, not the newest revision."""
        self._revision("My friend's write-up", editor=self.friend)
        self._revision("A stranger's later rewrite", editor=self.stranger)

        shown = self._shown()

        self.assertEqual(shown.content, "My friend's write-up")
        # Positive first: without it the negative below passes against the
        # empty string baker leaves on content_html, proving only that
        # rendering never ran.
        self.assertIn("<p>My friend's write-up</p>", shown.content_html)
        self.assertNotIn("stranger", shown.content_html)

    def test_the_projection_refuses_to_be_saved(self) -> None:
        """Saving it would publish an older revision as the current article."""
        self._revision("My friend's write-up", editor=self.friend)
        self._revision("A stranger's later rewrite", editor=self.stranger)

        with self.assertRaises(TypeError):
            self._shown().save()

    def test_an_unconcealed_viewer_reads_the_live_article(self) -> None:
        """The ordinary path, and the control for every assertion above."""
        self._revision("A stranger's later rewrite", editor=self.stranger)

        self.assertIs(conceal_article(self.article, self.wiki, self.viewer), self.article)


class ByIdScopeTests(TestCase):
    """The queryset every by-id lookup on a wiki-scoped row is now scoped to.

    Fifteen call sites looked up a comment, alias, link, edit, photo or article
    revision by id, scoped to the wiki rather than to the viewer. That is an
    existence oracle - "is there a row N here" is answered for rows concealment
    had already decided the account cannot see - and on the mutating routes it
    let the account act on one: reverting an edit it was never shown, deleting
    a stranger's alias, promoting one to the wiki's name.

    They all go through ``visible_rows`` now, so this covers the property they
    share rather than fifteen views separately.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        self.viewer = baker.make(User).profile
        self.friend = baker.make(User).profile
        self.stranger = baker.make(User).profile
        baker.make(Friendship, from_profile=self.viewer, to_profile=self.friend, status=FriendshipStatus.ACCEPTED)

        self.mine = baker.make(WikiEdit, wiki=self.wiki, editor=self.viewer)
        self.friends = baker.make(WikiEdit, wiki=self.wiki, editor=self.friend)
        self.theirs = baker.make(WikiEdit, wiki=self.wiki, editor=self.stranger)

    def _scoped(self):
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            return visible_rows(WikiEdit.objects.filter(wiki=self.wiki), self.wiki, self.viewer)

    def test_a_strangers_row_is_not_reachable_by_id(self) -> None:
        """The oracle, stated as the lookup that used to succeed."""
        self.assertFalse(self._scoped().filter(id=self.theirs.id).exists())

    def test_your_own_row_is_still_reachable_by_id(self) -> None:
        """Scoping must not cost the viewer their own rows - every route here is one they use."""
        self.assertTrue(self._scoped().filter(id=self.mine.id).exists())

    def test_a_friends_row_is_still_reachable_by_id(self) -> None:
        """Same rule as everywhere else: friends' contributions stay visible."""
        self.assertTrue(self._scoped().filter(id=self.friends.id).exists())

    def test_an_unconcealed_viewer_reaches_every_row(self) -> None:
        """The control. Without it these assertions pass against an empty queryset."""
        rows = visible_rows(WikiEdit.objects.filter(wiki=self.wiki), self.wiki, self.viewer)

        self.assertEqual(rows.count(), 3)
