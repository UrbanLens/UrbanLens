"""Tests for pin/wiki articles: rendering, sanitization, revisions, views, search."""

from __future__ import annotations

from django.urls import reverse
from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.articles import diff_revisions, render_article, save_article
from urbanlens.dashboard.services.global_search import GlobalSearchEngine


class RenderArticleTests(TestCase):
    """Markdown -> sanitized HTML rendering."""

    def test_empty_content_renders_empty(self) -> None:
        rendered = render_article("")
        self.assertEqual(rendered.html, "")
        self.assertEqual(rendered.toc, [])

    def test_headings_are_demoted_and_anchored(self) -> None:
        rendered = render_article("# Top\n\n## Section")
        self.assertIn('<h2 id="top">Top</h2>', rendered.html)
        self.assertIn('<h3 id="section">Section</h3>', rendered.html)
        self.assertNotIn("<h1", rendered.html)
        self.assertEqual([(entry.level, entry.anchor) for entry in rendered.toc], [(2, "top"), (3, "section")])

    def test_duplicate_headings_get_unique_anchors(self) -> None:
        rendered = render_article("## History\n\n## History")
        anchors = [entry.anchor for entry in rendered.toc]
        self.assertEqual(len(anchors), len(set(anchors)))

    def test_script_tags_are_stripped(self) -> None:
        rendered = render_article("hello <script>alert(1)</script> world")
        self.assertNotIn("<script", rendered.html)
        self.assertNotIn("alert(1)", rendered.html)

    def test_event_handler_attributes_are_stripped(self) -> None:
        rendered = render_article('<a href="https://x.example" onclick="evil()">link</a>')
        self.assertNotIn("onclick", rendered.html)

    def test_javascript_urls_never_become_links(self) -> None:
        # markdown-it's link validator refuses the javascript: URL outright,
        # leaving it as plain text; nh3 would strip the scheme regardless.
        rendered = render_article("[click](javascript:alert(1))")
        self.assertNotIn('href="javascript', rendered.html)
        self.assertNotIn("<a", rendered.html)

    def test_external_links_open_in_new_tab(self) -> None:
        rendered = render_article("[site](https://example.com)")
        self.assertIn('target="_blank"', rendered.html)
        self.assertIn("noopener", rendered.html)

    def test_footnotes_become_references_section(self) -> None:
        rendered = render_article("A fact.[^1]\n\n[^1]: The source.")
        self.assertTrue(rendered.has_references)
        self.assertIn("References", rendered.html)
        self.assertIn("footnotes-list", rendered.html)
        self.assertEqual(rendered.toc[-1].anchor, "article-references")

    def test_tables_render(self) -> None:
        rendered = render_article("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table>", rendered.html)
        self.assertIn("<td>1</td>", rendered.html)

    @hyp_settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
    @given(st.text(max_size=400))
    def test_arbitrary_text_never_produces_script_or_handlers(self, text: str) -> None:
        rendered = render_article(text)
        self.assertNotIn("<script", rendered.html.lower())
        self.assertNotIn("onerror=", rendered.html.lower())
        self.assertNotIn("javascript:", rendered.html.lower())


class DiffRevisionsTests(TestCase):
    """Line diffs between revision bodies."""

    def test_added_and_removed_lines(self) -> None:
        rows = diff_revisions("a\nb\nc", "a\nB\nc")
        kinds = [row.kind for row in rows]
        self.assertIn("del", kinds)
        self.assertIn("add", kinds)

    def test_identical_content_has_no_rows(self) -> None:
        self.assertEqual(diff_revisions("same\ntext", "same\ntext"), [])

    @hyp_settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
    @given(st.text(max_size=200), st.text(max_size=200))
    def test_diff_never_raises(self, old: str, new: str) -> None:
        rows = diff_revisions(old, new)
        for row in rows:
            self.assertIn(row.kind, {"context", "add", "del", "skip"})


class SaveArticleTests(TestCase):
    """save_article: creation, revisions, no-op saves, restores."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)

    def test_first_save_creates_article_and_revision(self) -> None:
        article, revision = save_article(editor=self.profile, content="## History\n\nBuilt 1900.", pin=self.pin)
        self.assertIsNotNone(revision)
        self.assertEqual(article.pin_id, self.pin.id)
        self.assertIn("Built 1900", article.content_html)
        self.assertEqual(article.revisions.count(), 1)
        self.assertEqual(article.last_edited_by, self.profile)
        self.assertEqual(article.toc[0]["anchor"], "history")

    def test_identical_save_is_a_noop(self) -> None:
        save_article(editor=self.profile, content="Same text.", pin=self.pin)
        _article, revision = save_article(editor=self.profile, content="Same text.", pin=self.pin)
        self.assertIsNone(revision)
        self.assertEqual(ArticleRevision.objects.count(), 1)

    def test_second_save_appends_revision(self) -> None:
        save_article(editor=self.profile, content="v1", pin=self.pin)
        article, _ = save_article(editor=self.profile, content="v2", edit_summary="tweak", pin=self.pin)
        self.assertEqual(article.revisions.count(), 2)
        self.assertEqual(article.content, "v2")
        latest = article.revisions.order_by("-created").first()
        self.assertEqual(latest.edit_summary, "tweak")

    def test_requires_exactly_one_host(self) -> None:
        with self.assertRaises(ValueError):
            save_article(editor=self.profile, content="x")

    def test_wiki_article_save(self) -> None:
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location, name="Mill Wiki")
        article, revision = save_article(editor=self.profile, content="Community text.", wiki=wiki)
        self.assertIsNotNone(revision)
        self.assertEqual(article.wiki_id, wiki.id)
        self.assertFalse(article.is_private)


class PinArticleViewTests(TestCase):
    """Pin article endpoints: privacy scoping, save flow, history, restore."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)
        self.other_user = baker.make("auth.User")
        self.client.force_login(self.user)

    def test_panel_renders_empty_state(self) -> None:
        response = self.client.get(reverse("pin.article", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "article for this pin")

    def test_save_creates_article_and_returns_panel(self) -> None:
        response = self.client.post(
            reverse("pin.article.save", args=[self.pin.slug]),
            {"content": "## History\n\nBuilt in 1900.", "edit_summary": "first draft", "base_revision_id": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Built in 1900")
        article = Article.objects.get(pin=self.pin)
        self.assertEqual(article.revisions.count(), 1)

    def test_stale_base_revision_is_rejected_with_409(self) -> None:
        save_article(editor=self.profile, content="v1", pin=self.pin)
        response = self.client.post(
            reverse("pin.article.save", args=[self.pin.slug]),
            {"content": "conflicting text", "base_revision_id": ""},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Article.objects.get(pin=self.pin).content, "v1")

    def test_other_users_cannot_see_pin_article(self) -> None:
        save_article(editor=self.profile, content="secret notes", pin=self.pin)
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("pin.article", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_history_lists_revisions(self) -> None:
        save_article(editor=self.profile, content="v1", pin=self.pin)
        save_article(editor=self.profile, content="v2 longer text", pin=self.pin)
        response = self.client.get(reverse("pin.article.history", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "#2")

    def test_restore_creates_new_revision_with_old_content(self) -> None:
        _, revision_one = save_article(editor=self.profile, content="v1", pin=self.pin)
        save_article(editor=self.profile, content="v2", pin=self.pin)
        response = self.client.post(reverse("pin.article.restore", args=[self.pin.slug, revision_one.id]))
        self.assertEqual(response.status_code, 200)
        article = Article.objects.get(pin=self.pin)
        self.assertEqual(article.content, "v1")
        self.assertEqual(article.revisions.count(), 3)
        newest = article.revisions.order_by("-created").first()
        self.assertEqual(newest.restored_from_id, revision_one.id)

    def test_diff_view_renders(self) -> None:
        save_article(editor=self.profile, content="line one", pin=self.pin)
        _, revision_two = save_article(editor=self.profile, content="line one\nline two", pin=self.pin)
        response = self.client.get(reverse("pin.article.revision", args=[self.pin.slug, revision_two.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "line two")

    def test_preview_returns_rendered_html_without_saving(self) -> None:
        response = self.client.post(
            reverse("pin.article.preview", args=[self.pin.slug]),
            {"content": "**bold** preview"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<strong>bold</strong>")
        self.assertFalse(Article.objects.filter(pin=self.pin).exists())

    def test_meta_bar_is_gone_but_edit_button_still_works(self) -> None:
        """The privacy/word-count/last-edited/revision-count meta bar was removed
        as redundant now the pin detail page always shows an Edit History tab -
        but the Edit button it also contained must still be reachable."""
        save_article(editor=self.profile, content="Built in 1900. Some history here.", pin=self.pin)
        response = self.client.get(reverse("pin.article", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "article-meta-bar")
        self.assertNotContains(response, "Last edited")
        self.assertNotContains(response, "word")
        self.assertContains(response, "article-edit-btn")
        self.assertContains(response, reverse("pin.article.edit", args=[self.pin.slug]))


class WikiArticleViewTests(TestCase):
    """Wiki article endpoints follow the standard wiki visibility gate."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Mill Wiki")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="My Mill Pin", name_is_user_provided=True)
        self.outsider = baker.make("auth.User")
        self.client.force_login(self.user)

    def test_pinned_user_can_view_and_save(self) -> None:
        response = self.client.post(
            reverse("location.wiki.article.save", args=[self.location.slug]),
            {"content": "Community history.", "base_revision_id": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Community history")
        self.assertTrue(Article.objects.filter(wiki=self.wiki).exists())

    def test_unpinned_user_gets_404(self) -> None:
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("location.wiki.article", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_second_pinned_user_can_edit_community_article(self) -> None:
        save_article(editor=self.profile, content="v1", wiki=self.wiki)
        second_user = baker.make("auth.User")
        second_profile = Profile.objects.get(user=second_user)
        baker.make(Pin, profile=second_profile, location=self.location, name="Their Pin", name_is_user_provided=True)
        self.client.force_login(second_user)
        latest = ArticleRevision.objects.order_by("-created").first()
        response = self.client.post(
            reverse("location.wiki.article.save", args=[self.location.slug]),
            {"content": "v2 by someone else", "base_revision_id": str(latest.id)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Article.objects.get(wiki=self.wiki).content, "v2 by someone else")


class ArticleSearchTests(TestCase):
    """Articles are searchable through global search, with access scoping."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Willow Mill", name_is_user_provided=True)
        save_article(editor=self.profile, content="The ancient turbine hall still stands.", pin=self.pin)

    def _articles_group(self, response):
        for group in response.groups:
            if group.meta.slug == "articles":
                return group.results
        return []

    def test_finds_pin_article_by_content(self) -> None:
        response = GlobalSearchEngine().search(self.profile, "ancient turbine")
        results = self._articles_group(response)
        self.assertTrue(results)
        self.assertIn("Willow Mill", results[0].title)
        self.assertIn("#tab-article", results[0].url)

    def test_other_users_cannot_find_private_pin_articles(self) -> None:
        other_user = baker.make("auth.User")
        other_profile = Profile.objects.get(user=other_user)
        response = GlobalSearchEngine().search(other_profile, "ancient turbine")
        self.assertEqual(self._articles_group(response), [])

    def test_wiki_article_found_by_pinned_user(self) -> None:
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location, name="Shared Mill")
        baker.make(Pin, profile=self.profile, location=location, name="Mine", name_is_user_provided=True)
        save_article(editor=self.profile, content="Community boiler room notes.", wiki=wiki)
        response = GlobalSearchEngine().search(self.profile, "boiler room")
        results = self._articles_group(response)
        self.assertTrue(any("Shared Mill" in result.title for result in results))

    def test_articles_type_keyword_focuses_search(self) -> None:
        response = GlobalSearchEngine().search(self.profile, "articles about turbine")
        slugs = [group.meta.slug for group in response.groups]
        self.assertEqual(slugs, ["articles"])
