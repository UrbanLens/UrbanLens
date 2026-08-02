"""Article read/save/history on the external API's wiki surface.

The behavior under the most scrutiny here is optimistic concurrency. Two people
editing one wiki article is the normal case, not the exceptional one, and a save
that silently overwrites the other person's work is unrecoverable from the
client's side. So a stale (or absent) ``base_revision_id`` must refuse the
write, and must refuse it *without* recording a revision.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.wiki.articles import get_article, save_article
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"


class WikiArticleTestCase(TestCase):
    """Shared fixture: one visible wiki, optionally with an article."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Article client")
        grant_wiki_scopes(self.user)

        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def url(self, suffix: str = "") -> str:
        return f"{BASE}/{self.location.ensure_slug()}/article/{suffix}"

    def put(self, payload: dict):
        return self.client.put(self.url(), payload, content_type="application/json", **self.headers())


class ArticleReadTests(WikiArticleTestCase):
    """GET the article."""

    def test_absent_article_is_the_uniform_404(self) -> None:
        response = self.client.get(self.url(), **self.headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_article_body_and_base_revision_are_returned(self) -> None:
        save_article(editor=self.profile, content="# Heading\n\nBody text", wiki=self.wiki)
        body = self.client.get(self.url(), **self.headers()).json()

        self.assertIn("Body text", body["content"])
        # The renderer deliberately demotes a leading "#" to <h2> - the page
        # title owns the document's only <h1>.
        self.assertIn("<h2", body["content_html"])
        self.assertIsNotNone(body["base_revision_id"])


class ArticleConflictTests(WikiArticleTestCase):
    """A save may never silently clobber a concurrent edit."""

    def setUp(self) -> None:
        super().setUp()
        _article, self.first_revision = save_article(editor=self.profile, content="Original body", wiki=self.wiki)

    def test_correct_base_revision_saves(self) -> None:
        response = self.put({"content": "Updated body", "base_revision_id": self.first_revision.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_article(wiki=self.wiki).content, "Updated body")

    def test_stale_base_revision_is_a_409_naming_the_current_revision(self) -> None:
        _article, newer = save_article(editor=self.profile, content="Someone else's edit", wiki=self.wiki)

        response = self.put({"content": "My doomed edit", "base_revision_id": self.first_revision.pk})
        self.assertEqual(response.status_code, 409)

        body = response.json()
        self.assertTrue(body["conflict"])
        self.assertEqual(body["current_revision_id"], newer.pk)

    def test_no_revision_is_written_on_conflict(self) -> None:
        """The refusal must leave the history untouched."""
        save_article(editor=self.profile, content="Someone else's edit", wiki=self.wiki)
        before = ArticleRevision.objects.count()

        self.put({"content": "My doomed edit", "base_revision_id": self.first_revision.pk})

        self.assertEqual(ArticleRevision.objects.count(), before)
        self.assertEqual(get_article(wiki=self.wiki).content, "Someone else's edit")

    def test_null_base_revision_conflicts_when_revisions_exist(self) -> None:
        """"I think this article is new" is wrong here, so the save is refused."""
        response = self.put({"content": "Blind write", "base_revision_id": None})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(get_article(wiki=self.wiki).content, "Original body")

    def test_omitted_base_revision_is_rejected(self) -> None:
        """Unlike the internal form, the field is required - see serializers_wiki.

        Omitting it is a 400 (a missing required field), not a 409; either way
        the write is refused, which is the property that matters.
        """
        response = self.put({"content": "Blind write"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_article(wiki=self.wiki).content, "Original body")

    def test_first_ever_save_accepts_a_null_base_revision(self) -> None:
        """On a wiki with no article yet, null is the honest answer."""
        other_location = baker.make("dashboard.Location")
        other_wiki = baker.make("dashboard.Wiki", location=other_location, name="Fresh")
        baker.make("dashboard.Pin", profile=self.profile, location=other_location)

        response = self.client.put(
            f"{BASE}/{other_location.ensure_slug()}/article/",
            {"content": "Brand new", "base_revision_id": None},
            content_type="application/json",
            **self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_article(wiki=other_wiki).content, "Brand new")


class ArticleRevisionTests(WikiArticleTestCase):
    """Revision list, detail-with-diff, and restore."""

    def setUp(self) -> None:
        super().setUp()
        _a, self.rev1 = save_article(editor=self.profile, content="First version", wiki=self.wiki)
        _b, self.rev2 = save_article(editor=self.profile, content="Second version", wiki=self.wiki)

    def test_revisions_are_listed_newest_first(self) -> None:
        rows = self.client.get(self.url("revisions/"), **self.headers()).json()["results"]
        self.assertEqual([row["id"] for row in rows], [self.rev2.pk, self.rev1.pk])

    def test_revision_detail_includes_a_diff_against_its_predecessor(self) -> None:
        body = self.client.get(self.url(f"revisions/{self.rev2.pk}/"), **self.headers()).json()
        self.assertEqual(body["content"], "Second version")
        self.assertTrue(body["diff"])

    def test_restore_appends_rather_than_rewriting_history(self) -> None:
        before = ArticleRevision.objects.filter(article__wiki=self.wiki).count()

        response = self.client.post(self.url(f"revisions/{self.rev1.pk}/restore/"), **self.headers())
        self.assertEqual(response.status_code, 200)

        self.assertEqual(get_article(wiki=self.wiki).content, "First version")
        self.assertEqual(ArticleRevision.objects.filter(article__wiki=self.wiki).count(), before + 1)
        # The originals are still there - restoring is not a rollback.
        self.assertTrue(ArticleRevision.objects.filter(pk=self.rev2.pk).exists())
