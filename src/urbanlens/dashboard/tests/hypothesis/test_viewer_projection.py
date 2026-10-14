"""The seam that decides how a viewer reads a wiki: the identity by default, a plugin's projection when one is contributed."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins import points
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.map_pins.autocomplete import search_local
from urbanlens.dashboard.services.photos.albums import albums_listing
from urbanlens.dashboard.services.wiki.viewer_projection import (
    ViewerProjection,
    is_projected,
    project_article,
    project_wiki,
    projection_of,
    viewer_projection,
    visible_rows,
    writable_wiki,
)
from urbanlens.dashboard.tests.hypothesis.viewer_projection_helpers import (
    PROJECTED_NAME,
    failing_projection,
    projecting,
)


class _Fixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = baker.make(User).profile
        self.other = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.viewer, location=self.location)


class IdentityTests(_Fixture):
    """With nothing contributed, every read shows the stored rows."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(points.WIKI_VIEWER_PROJECTION.overridden([]))

    def test_the_projection_changes_nothing(self) -> None:
        self.assertFalse(viewer_projection(self.wiki, self.viewer).active)

    def test_the_wiki_is_the_row_itself(self) -> None:
        self.assertIs(project_wiki(self.wiki, self.viewer), self.wiki)
        self.assertFalse(is_projected(self.wiki))

    def test_rows_are_not_narrowed(self) -> None:
        baker.make(Comment, wiki=self.wiki, pin=None, profile=self.other)
        rows = Comment.objects.filter(wiki=self.wiki)

        self.assertEqual(visible_rows(rows, self.wiki, self.viewer).count(), 1)

    def test_the_article_is_the_row_itself(self) -> None:
        article = baker.make(Article, wiki=self.wiki, pin=None, content="Text")

        self.assertIs(project_article(article, self.wiki, self.viewer), article)
        self.assertIsNone(project_article(None, self.wiki, self.viewer))

    def test_a_writable_wiki_is_the_row_itself(self) -> None:
        self.assertIs(writable_wiki(self.wiki), self.wiki)

    def test_search_and_autocomplete_find_the_stored_name(self) -> None:
        response = GlobalSearchEngine().search(self.viewer, "mill")

        titles = [result.title for group in response.groups if group.meta.slug == "wikis" for result in group.results]
        self.assertEqual(titles, ["Old Mill"])
        self.assertIn("Old Mill", [result.title for result in search_local("mill", self.viewer)])


class FailClosedTests(_Fixture):
    """A projection that errors must not fall back to showing the stored rows to a viewer it should have narrowed."""

    def test_the_seam_raises_rather_than_skips(self) -> None:
        self.assertEqual(points.WIKI_VIEWER_PROJECTION.on_error, "raise")

    def test_every_read_surfaces_the_error(self) -> None:
        with failing_projection(RuntimeError("projection broke")):
            for read in (
                lambda: viewer_projection(self.wiki, self.viewer),
                lambda: project_wiki(self.wiki, self.viewer),
                lambda: visible_rows(Comment.objects.filter(wiki=self.wiki), self.wiki, self.viewer),
                lambda: project_article(None, self.wiki, self.viewer),
            ):
                with self.subTest(read=read), self.assertRaisesMessage(RuntimeError, "projection broke"):
                    read()


class ProjectedWikiTests(_Fixture):
    """The wiki a projection hands back, and what may be done with it."""

    def test_the_wiki_reads_as_the_projection_shows_it(self) -> None:
        with projecting():
            shown = project_wiki(self.wiki, self.viewer)

        self.assertEqual(shown.name, PROJECTED_NAME)
        self.assertEqual(shown.pk, self.wiki.pk)
        self.assertIsInstance(shown, Wiki)
        self.assertTrue(is_projected(shown))

    def test_the_stored_row_is_untouched(self) -> None:
        with projecting():
            project_wiki(self.wiki, self.viewer)

        self.assertEqual(self.wiki.name, "Old Mill")
        self.assertEqual(Wiki.objects.get(pk=self.wiki.pk).name, "Old Mill")

    def test_a_projection_refuses_to_be_written(self) -> None:
        with projecting():
            shown = project_wiki(self.wiki, self.viewer)

        with self.assertRaises(TypeError):
            shown.save()
        with self.assertRaises(TypeError):
            shown.delete()

    def test_a_writable_wiki_is_the_stored_row(self) -> None:
        with projecting():
            shown = project_wiki(self.wiki, self.viewer)

        target = writable_wiki(shown)

        self.assertFalse(is_projected(target))
        self.assertEqual(target.name, "Old Mill")
        self.assertEqual(target.pk, self.wiki.pk)

    def test_projecting_again_for_the_same_viewer_reuses_the_projection(self) -> None:
        with projecting():
            shown = project_wiki(self.wiki, self.viewer)
            again = project_wiki(shown, self.viewer)

        self.assertIs(again, shown)

    def test_projecting_for_another_viewer_rebuilds_from_the_stored_row(self) -> None:
        with projecting(lambda wiki, viewer: viewer is not None and viewer.pk == self.viewer.pk):
            shown = project_wiki(self.wiki, self.viewer)
            for_other = project_wiki(shown, self.other)

        self.assertFalse(is_projected(for_other))
        self.assertEqual(for_other.name, "Old Mill")

    def test_a_signed_out_viewer_is_not_mistaken_for_one_the_projection_was_built_for(self) -> None:
        with projecting():
            shown = project_wiki(self.wiki, self.viewer)
            for_nobody = project_wiki(shown, None)

        self.assertIsNot(for_nobody, shown)
        self.assertIsNone(projection_of(for_nobody).viewer)


class ProjectedRowsTests(_Fixture):
    def test_rows_the_projection_hides_are_left_out(self) -> None:
        mine = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.viewer)
        theirs = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.other)

        with projecting(hidden_profile_ids=[self.other.pk]):
            rows = visible_rows(Comment.objects.filter(wiki=self.wiki), self.wiki, self.viewer)

        self.assertEqual(list(rows), [mine])
        self.assertNotIn(theirs, rows)

    def test_a_projection_for_another_wiki_leaves_these_rows_alone(self) -> None:
        baker.make(Comment, wiki=self.wiki, pin=None, profile=self.other)

        with projecting(lambda wiki, viewer: wiki.pk != self.wiki.pk, hidden_profile_ids=[self.other.pk]):
            rows = visible_rows(Comment.objects.filter(wiki=self.wiki), self.wiki, self.viewer)

        self.assertEqual(rows.count(), 1)

    def test_the_newest_revision_shown_skips_the_ones_hidden(self) -> None:
        article = baker.make(Article, wiki=self.wiki, pin=None, content="Newest")
        shown = baker.make(ArticleRevision, article=article, content="Visible", editor=self.viewer)
        baker.make(ArticleRevision, article=article, content="Hidden", editor=self.other)

        with projecting(hidden_profile_ids=[self.other.pk]):
            revision = viewer_projection(self.wiki, self.viewer).newest_revision(article)
            projected = project_article(article, self.wiki, self.viewer)

        self.assertEqual(revision, shown)
        assert projected is not None
        self.assertEqual(projected.content, "Visible")
        self.assertTrue(is_projected(projected))
        with self.assertRaises(TypeError):
            projected.save()


class AlbumListingTests(_Fixture):
    """Albums listed across several wikis are narrowed only where the viewer's projection applies."""

    def test_each_wikis_albums_are_narrowed_on_its_own_terms(self) -> None:
        other_location = baker.make(Location)
        other_wiki = baker.make(Wiki, location=other_location, name="Granary")
        mine = baker.make(Album, parent_wiki=self.wiki, name="Mine", profile=self.viewer)
        theirs_here = baker.make(Album, parent_wiki=self.wiki, name="Theirs here", profile=self.other)
        theirs_there = baker.make(Album, parent_wiki=other_wiki, name="Theirs there", profile=self.other)

        with projecting(lambda wiki, viewer: wiki.pk == self.wiki.pk, hidden_profile_ids=[self.other.pk]):
            listed = [entry.album for entry in albums_listing([self.wiki, other_wiki], self.viewer)]

        self.assertCountEqual(listed, [mine, theirs_there])
        self.assertNotIn(theirs_here, listed)

    def test_a_single_wiki_is_narrowed_wholesale(self) -> None:
        mine = baker.make(Album, parent_wiki=self.wiki, name="Mine", profile=self.viewer)
        baker.make(Album, parent_wiki=self.wiki, name="Theirs", profile=self.other)

        with projecting(hidden_profile_ids=[self.other.pk]):
            listed = [entry.album for entry in albums_listing(self.wiki, self.viewer)]

        self.assertEqual(listed, [mine])


class SearchTests(_Fixture):
    """A candidate the SQL match found on the stored text is kept only if the viewer's projection still matches it."""

    def setUp(self) -> None:
        super().setUp()
        self.projected = baker.make(Wiki, location=baker.make(Location), name="Projected Mill")
        baker.make(Pin, profile=self.viewer, location=self.projected.location)

    def _wiki_titles(self, query: str) -> list[str]:
        response = GlobalSearchEngine().search(self.viewer, query)
        return [result.title for group in response.groups if group.meta.slug == "wikis" for result in group.results]

    def test_a_candidate_the_projection_does_not_match_is_dropped(self) -> None:
        with projecting():
            titles = self._wiki_titles("old mill")

        self.assertEqual(titles, [])

    def test_a_candidate_the_projection_matches_is_titled_as_the_viewer_reads_it(self) -> None:
        with projecting():
            titles = self._wiki_titles("projected")

        self.assertEqual(titles, [PROJECTED_NAME])

    def test_autocomplete_drops_and_retitles_the_same_way(self) -> None:
        with projecting():
            dropped = [result.title for result in search_local("old mill", self.viewer)]
            kept = [result.title for result in search_local("projected", self.viewer)]

        self.assertNotIn("Old Mill", dropped)
        self.assertIn(PROJECTED_NAME, kept)


class ProjectionBaseTests(_Fixture):
    """What the identity projection answers, for a plugin that overrides only some of it."""

    def test_every_extra_is_shown(self) -> None:
        projection = ViewerProjection(self.wiki, self.viewer)

        self.assertTrue(projection.shows_cover_photo())
        self.assertTrue(projection.shows_wiki_outline())
        self.assertTrue(projection.shows_build_year())
        self.assertTrue(projection.shows_wiki_link())
        self.assertTrue(projection.shows_change(self.other, ["cameras"]))
        self.assertTrue(projection.matches_wiki_text(["anything"]))

    def test_an_edit_diff_is_returned_as_stored(self) -> None:
        changes = {"name": {"from": "A", "to": "B"}}

        self.assertIs(ViewerProjection(self.wiki, self.viewer).edit_changes(changes), changes)
