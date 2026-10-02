"""Tests for pin/wiki articles: rendering, sanitization, revisions, views, search."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.wiki.articles import diff_revisions, render_article, save_article


class EditorDisplayNameTests(SimpleTestCase):
    """ArticleRevision.editor_display_name - regression coverage for the "'Deleted user' shown for a Wikipedia-seeded starting article" report: a null ``editor`` means either a genuinely deleted account or a system-initiated seed (services.wiki.wiki_seed passes editor=None on purpose) - these must not both show "Deleted user"."""

    def test_editor_present_returns_username(self) -> None:
        profile = Profile(user=User(username="alice"))
        revision = ArticleRevision(editor=profile)
        self.assertEqual(revision.editor_display_name, "alice")

    def test_no_editor_and_no_summary_returns_deleted_user(self) -> None:
        revision = ArticleRevision(editor=None, edit_summary="")
        self.assertEqual(revision.editor_display_name, "Deleted user")

    def test_no_editor_with_unrelated_summary_returns_deleted_user(self) -> None:
        revision = ArticleRevision(editor=None, edit_summary="Fixed a typo")
        self.assertEqual(revision.editor_display_name, "Deleted user")

    def test_no_editor_with_wikipedia_seed_summary_returns_wikipedia(self) -> None:
        revision = ArticleRevision(editor=None, edit_summary="Seeded from Wikipedia")
        self.assertEqual(revision.editor_display_name, "Wikipedia")


class RenderArticleTests(SimpleTestCase):
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


class DiffRevisionsTests(SimpleTestCase):
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

    def test_rejects_both_hosts_at_once(self) -> None:
        """The neither- and both-hosts branches share one condition (`(pin is None) == (wiki
        is None)`) - a regression that only checked for "neither" would still pass the test
        above while silently accepting a pin+wiki call it should reject."""
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location, name="Both Mill")
        with self.assertRaises(ValueError):
            save_article(editor=self.profile, content="x", pin=self.pin, wiki=wiki)

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
        response = self.client.get(reverse("pin.article", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200, "the owner must still be able to see it")
        self.assertContains(response, "secret notes")

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

    def test_meta_bar_is_gone(self) -> None:
        """The privacy/word-count/last-edited/revision-count meta bar was removed
        as redundant now the Private Pin page always shows an Edit History tab."""
        save_article(editor=self.profile, content="Built in 1900. Some history here.", pin=self.pin)
        response = self.client.get(reverse("pin.article", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "article-meta-bar")
        self.assertNotContains(response, "Last edited")
        self.assertNotContains(response, "word")

    def test_panel_is_always_the_editable_canvas_no_edit_click_needed(self) -> None:
        """Notion-style: the Article tab is always the WYSIWYG canvas itself, with
        no separate read-only mode and no Edit button to click through first -
        see frontend/ts/entries/article-wysiwyg.ts."""
        save_article(editor=self.profile, content="Built in 1900. Some history here.", pin=self.pin)
        response = self.client.get(reverse("pin.article", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-article-editor")
        self.assertContains(response, "data-article-canvas")
        self.assertContains(response, "data-article-textarea")
        self.assertContains(response, 'name="content"')
        self.assertNotContains(response, "article-edit-btn")

    def test_pin_detail_page_loads_the_wysiwyg_editor_script(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dashboard/js/article-wysiwyg.js")

    def test_pin_detail_page_offers_source_and_clear_via_the_actions_menu(self) -> None:
        """Source/Clear live in the pin-detail actions menu (_hierarchy_actions_fab.html), not a floating toolbar inside the article panel itself - see editorRootForControl() in article-wysiwyg.ts for how they still reach the editor from outside its own DOM subtree."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-article-mode-toggle")
        self.assertContains(response, "data-article-clear")
        self.assertContains(response, 'id="pin-actions-fab"')


class WikiArticleViewTests(TestCase):
    """Wiki article endpoints follow the standard wiki visibility gate."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Mill Wiki")
        self.pin = baker.make(
            Pin, profile=self.profile, location=self.location, name="My Mill Pin", name_is_user_provided=True
        )
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
        response = self.client.get(reverse("location.wiki.article", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200, "a pinned user must still be able to see it")

        self.client.force_login(self.outsider)
        response = self.client.get(reverse("location.wiki.article", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_page_loads_the_wysiwyg_editor_script(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dashboard/js/article-wysiwyg.js")

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


class ArticleImagesAreLocalTests(TestCase):
    """An image written into an article is shown from this site's copy, never its host (P165; Jess, 2026-09-29)."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")

    def _copy(self, url: str) -> str:
        from urbanlens.dashboard.services.media.remote_copies import url_digest

        return reverse("media.remote_copy", args=[url_digest(url)])

    def test_rendering_shows_copies_for_markdown_and_html_images(self) -> None:
        html = render_article(
            '![Mill](https://img.example/mill.jpg?a=1&b=2)\n\n<img src="https://img.example/raw.png" alt="raw">\n\n![Local](/media/x.jpg)'
        ).html

        self.assertNotIn("img.example", html)
        self.assertIn(f'src="{self._copy("https://img.example/mill.jpg?a=1&b=2")}"', html)
        self.assertIn(f'src="{self._copy("https://img.example/raw.png")}"', html)
        self.assertIn('src="/media/x.jpg"', html)

    def test_saving_stores_the_copy_in_the_source_the_editor_loads(self) -> None:
        from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy

        article, _revision = save_article(
            content='Intro\n\n![Mill](https://img.example/mill.jpg "The mill")\n\n<img src="https://img.example/raw.png">',
            pin=self.pin,
            editor=self.pin.profile,
        )

        self.assertNotIn("img.example", article.content)
        self.assertIn(f'![Mill]({self._copy("https://img.example/mill.jpg")} "The mill")', article.content)
        self.assertIn(f'<img src="{self._copy("https://img.example/raw.png")}">', article.content)
        self.assertEqual(
            set(RemoteImageCopy.objects.values_list("source_url", "provider")),
            {("https://img.example/mill.jpg", "article"), ("https://img.example/raw.png", "article")},
        )

    def test_code_that_shows_image_syntax_is_left_as_written(self) -> None:
        from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy

        sample = (
            "Write it like this:\n\n```\n![Mill](https://img.example/fenced.jpg)\n```\n\n"
            '    <img src="https://img.example/indented.png">\n\n'
            "Or inline: `![x](https://img.example/inline.jpg)`, then ![Real](https://img.example/real.jpg)"
        )
        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        for literal in ("fenced.jpg", "indented.png", "inline.jpg"):
            self.assertIn(f"https://img.example/{literal}", article.content)
        self.assertNotIn("https://img.example/real.jpg", article.content)
        self.assertEqual(
            list(RemoteImageCopy.objects.values_list("source_url", flat=True)), ["https://img.example/real.jpg"]
        )

    def test_code_spans_follow_their_own_backtick_count(self) -> None:
        sample = "``a ` ![x](https://img.example/inside.jpg)`` and ` then ![Real](https://img.example/real.jpg)"

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertIn("https://img.example/inside.jpg", article.content)
        self.assertNotIn("https://img.example/real.jpg", article.content)

    def test_a_link_to_an_image_stays_a_link(self) -> None:
        article, _revision = save_article(
            content="[the photo](https://img.example/mill.jpg)", pin=self.pin, editor=self.pin.profile
        )

        self.assertEqual(article.content, "[the photo](https://img.example/mill.jpg)")

    def test_the_command_moves_existing_articles_onto_copies_as_a_new_revision(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        article = Article.objects.create(pin=self.pin, content="![Mill](https://img.example/mill.jpg)")
        Article.objects.create(pin=baker.make_recipe("dashboard.pin"), content="No pictures here.")

        out = StringIO()
        call_command("localize_article_images", "--dry-run", stdout=out)
        self.assertIn("1 article(s) would change", out.getvalue())
        article.refresh_from_db()
        self.assertIn("img.example", article.content)

        call_command("localize_article_images", stdout=StringIO())
        article.refresh_from_db()
        self.assertEqual(article.content, f"![Mill]({self._copy('https://img.example/mill.jpg')})")
        self.assertNotIn("img.example", article.content_html)
        self.assertEqual(article.revisions.get().edit_summary, "Images stored on this site")

    def test_reference_style_images_store_copies_in_their_definitions(self) -> None:
        from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy

        sample = (
            "![The mill][Mill] and ![Yard][] and ![shortcut] and ![quoted]\n\n"
            '[mill]: https://img.example/mill.jpg "The mill"\n'
            "[yard]: <https://img.example/yard.png>\n"
            "[Shortcut]: https://img.example/short.jpg\n\n"
            "> [quoted]:\n> https://img.example/quoted.jpg"
        )

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertIn("![The mill][Mill] and ![Yard][] and ![shortcut] and ![quoted]", article.content)
        self.assertIn(f'[mill]: {self._copy("https://img.example/mill.jpg")} "The mill"', article.content)
        self.assertIn(f"[yard]: <{self._copy('https://img.example/yard.png')}>", article.content)
        self.assertIn(f"[Shortcut]: {self._copy('https://img.example/short.jpg')}", article.content)
        self.assertIn(f"> [quoted]:\n> {self._copy('https://img.example/quoted.jpg')}", article.content)
        self.assertNotIn("img.example", article.content)
        self.assertEqual(RemoteImageCopy.objects.get(source_url="https://img.example/mill.jpg").provider, "article")

    def test_a_definition_only_a_link_uses_stays_as_written(self) -> None:
        sample = (
            "[the full photo][full] beside ![thumb][small], and `![code][code]`\n\n"
            "[full]: https://img.example/full.jpg\n[small]: https://img.example/small.jpg\n"
            "[code]: https://img.example/code.jpg"
        )

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertIn("[full]: https://img.example/full.jpg", article.content)
        self.assertIn("[code]: https://img.example/code.jpg", article.content)
        self.assertNotIn("https://img.example/small.jpg", article.content)

    def test_an_escaped_bang_is_a_link_and_stays_one(self) -> None:
        sample = "\\![a link](https://img.example/a.jpg) and \\![a reference link][r]\n\n[r]: https://img.example/r.jpg"

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertEqual(article.content, sample)

    def test_markdown_inside_an_html_block_is_not_an_image(self) -> None:
        sample = '<div class="note">\n![shown as text](https://img.example/a.jpg)\n</div>'

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertEqual(article.content, sample)

    def test_addresses_with_parentheses_store_copies(self) -> None:
        from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy

        sample = (
            "![Mill](https://img.example/Mill_(1900).jpg)\n\n"
            '![Escaped](https://img.example/a\\(b.jpg "a title")\n\n'
            "![Spaced](<https://img.example/c (d).jpg>)\n\n"
            '<img src="https://img.example/e(f).png" alt="e">\n\n'
            "<img src='https://img.example/g(h) i.png'>"
        )

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertNotIn("img.example", article.content)
        self.assertIn('"a title")', article.content)
        self.assertEqual(
            set(RemoteImageCopy.objects.values_list("source_url", flat=True)),
            {
                "https://img.example/Mill_(1900).jpg",
                "https://img.example/a(b.jpg",
                "https://img.example/c%20(d).jpg",
                "https://img.example/e(f).png",
                "https://img.example/g(h) i.png",
            },
        )

    def test_a_parenthesis_that_ends_the_image_is_not_part_of_its_address(self) -> None:
        sample = "(see ![Mill](https://img.example/mill.jpg))"

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertEqual(article.content, f"(see ![Mill]({self._copy('https://img.example/mill.jpg')}))")

    def test_what_is_not_an_image_is_left_alone(self) -> None:
        sample = (
            "![no close](https://img.example/a.jpg\n\n"
            '![bad title](https://img.example/b.jpg "unterminated)\n\n'
            '![title in the next paragraph](https://img.example/c.jpg "\n\n")'
        )

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertEqual(article.content, sample)

    def test_the_stored_copy_is_the_one_rendering_would_have_shown(self) -> None:
        """Rendering keys a copy by the address the browser would load; a source rewritten under another key would
        make a second copy of the same picture and change nothing else."""
        sample = (
            "![a](https://img.example/x_(1).jpg) ![b](<https://img.example/sp ace.jpg>) ![c][r] "
            '![d](HTTPS://img.example/upper.jpg) <img src="https://img.example/q.png?a=1&amp;b=2">\n\n'
            "[r]: https://img.example/café.jpg"
        )
        before = render_article(sample).html

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertEqual(article.content_html, before)
        self.assertNotIn("img.example", before)

    def test_an_address_the_url_parser_refuses_saves(self) -> None:
        sample = 'An unparseable host: <img src="https://[x/a.jpg">'

        article, _revision = save_article(content=sample, pin=self.pin, editor=self.pin.profile)

        self.assertEqual(article.content, sample)

    def test_the_command_also_moves_reference_style_images(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        article = Article.objects.create(pin=self.pin, content="![Mill][m]\n\n[m]: https://img.example/mill.jpg")

        call_command("localize_article_images", stdout=StringIO())

        article.refresh_from_db()
        self.assertEqual(article.content, f"![Mill][m]\n\n[m]: {self._copy('https://img.example/mill.jpg')}")


_ALT = st.text(alphabet="abc XYZ", min_size=1, max_size=6).map(str.strip).filter(bool)
# Parentheses balanced: an unbalanced one ends the address early, and markdown-it's fallback for the inline syntax that
# then fails can swallow a neighbouring image into a link, which is its own quirk rather than this code's.
_PATH = st.lists(
    st.sampled_from(["a", "z", "0", "-", "_", ".", "(b)", "(c_(d))", "&", "=", "?", "é"]), min_size=1, max_size=8
).map("".join)
_IMAGE_LABELS = ("one", "Two", "three four")
_LINK_LABELS = ("l1", "L2")


@st.composite
def _article_sources(draw: st.DrawFn) -> str:
    """Article source mixing every way an image can be written with links and code that must stay as they are."""
    pieces: list[str] = []
    defined: dict[str, str] = {}
    for _ in range(draw(st.integers(min_value=1, max_value=6))):
        url = f"https://img.example/{draw(_PATH)}"
        alt = draw(_ALT)
        kind = draw(st.sampled_from(["inline", "bracketed", "titled", "html", "reference", "link", "linkref", "code"]))
        if kind == "inline":
            pieces.append(f"![{alt}]({url})")
        elif kind == "bracketed":
            pieces.append(f"![{alt}](<{url} x>)")
        elif kind == "titled":
            pieces.append(f'![{alt}]({url} "{alt}")')
        elif kind == "html":
            pieces.append(f'<img src="{url}" alt="{alt}">')
        elif kind == "reference":
            label = draw(st.sampled_from(_IMAGE_LABELS))
            defined.setdefault(label, url)
            pieces.append(draw(st.sampled_from([f"![{alt}][{label.upper()}]", f"![{label}][]", f"![{label}]"])))
        elif kind == "link":
            pieces.append(f"[{alt}]({url})")
        elif kind == "linkref":
            label = draw(st.sampled_from(_LINK_LABELS))
            defined.setdefault(label, url)
            pieces.append(f"[{alt}][{label}]")
        else:
            pieces.append(f"`![{alt}]({url})`")
    body = draw(st.sampled_from([" ", "\n", "\n\n"])).join(pieces)
    definitions = "\n".join(f"[{label}]: {url}" for label, url in defined.items())
    return f"{body}\n\n{definitions}" if definitions else body


class StoredSourceShowsWhatTheAuthorWroteTests(TestCase):
    """Rewriting image addresses in the source changes where the editor loads them from, and nothing else."""

    @hyp_settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_article_sources())
    def test_rendering_the_stored_source_matches_rendering_what_was_written(self, content: str) -> None:
        from unittest import mock

        from urbanlens.dashboard.services.wiki import articles

        localized = articles.localize_article_images(content)

        self.assertEqual(render_article(localized).html, render_article(content).html)
        with mock.patch.object(articles, "_localize_rendered_images", side_effect=lambda html: html):
            self.assertNotRegex(render_article(localized).html, r'<img [^>]*src="https?://')


class ArticleImageScanIsLinearTests(SimpleTestCase):
    """Every save runs the image scan in the request, so text built to make its patterns backtrack must not hold a
    worker. Before the fix 1,000 backticks took thirteen seconds, 2,000 four minutes, 20,000 ``<img `` fifty, and
    50,000 ``![`` ten."""

    def _assert_fast(self, content: str) -> None:
        import time
        from unittest import mock

        from urbanlens.dashboard.services.wiki import articles

        with mock.patch.object(articles, "_copies_of", side_effect=lambda urls: dict.fromkeys(urls, "/copy/")):
            started = time.perf_counter()
            articles.localize_article_images(content)
            self.assertLess(time.perf_counter() - started, 2.0)

    def test_a_long_run_of_backticks(self) -> None:
        self._assert_fast("`" * 1000)

    def test_backtick_runs_of_every_length(self) -> None:
        self._assert_fast(" ".join("`" * n for n in range(1, 600)))

    def test_many_unclosed_img_tags(self) -> None:
        self._assert_fast("<img " * 20000)

    def test_many_unclosed_image_brackets(self) -> None:
        self._assert_fast("![" * 50000)

    def test_unclosed_image_brackets_beside_a_definition(self) -> None:
        self._assert_fast("![" * 50000 + "\n\n[a]: https://img.example/a.jpg")

    def test_many_nested_parentheses(self) -> None:
        self._assert_fast("![a](https://img.example/" + "(" * 20000)

    def test_many_images_opening_parentheses(self) -> None:
        self._assert_fast("![a](https://img.example/x" * 20000)

    def test_many_unterminated_titles(self) -> None:
        self._assert_fast('![a](https://img.example/x "' * 20000)

    def test_many_unclosed_quoted_sources(self) -> None:
        self._assert_fast('<img src="https://img.example/' * 20000)

    def test_many_reference_images_and_definitions(self) -> None:
        self._assert_fast(
            " ".join(f"![x][a{n}]" for n in range(5000))
            + "\n\n"
            + "\n".join(f"[a{n}]: https://img.example/{n}.jpg" for n in range(5000))
        )

    def test_code_spans_beside_many_images(self) -> None:
        self._assert_fast("`a` " * 20000 + "![a](/local.jpg) " * 20000)
