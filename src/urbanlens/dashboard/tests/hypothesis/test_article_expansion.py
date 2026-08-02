"""Tests for AI article expansion from link extraction.

Covers plain-text sanitization, the fail-closed safety classifier, append
applies to pin and wiki articles, feature-flag skips, and the
run_extraction integration hook. Gateways are always mocked.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.link_extraction.model import LinkExtraction, LinkExtractionStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import SiteFeature
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.ai.article_expansion import (
    EDIT_SUMMARY_EXPANDED_FROM_LINK,
    expand_articles_from_page,
    sanitize_article_plain_text,
)
from urbanlens.dashboard.services.ai.article_safety import ArticleSafetyVerdict, classify_article_text
from urbanlens.dashboard.services.ai.link_extraction import run_extraction
from urbanlens.dashboard.services.wiki.articles import get_article, save_article
from urbanlens.dashboard.services.core.text_limits import MAX_ARTICLE_LENGTH


class _FakeGateway:
    """Minimal gateway stub whose ``send_prompt`` returns a fixed answer."""

    def __init__(self, answer: str | None, *, model: str = "fake-model"):
        self._answer = answer
        self.model = model
        self.prompts: list[str] = []

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        self.prompts.append(prompt)
        return self._answer

    @property
    def cost(self) -> Decimal:
        return Decimal("0.001")


def _grant_ai_to_everyone() -> None:
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(
        ai_enabled=True,
        ai_link_extraction_enabled=True,
        ai_article_expansion_enabled=True,
        ai_article_safety_enabled=True,
        default_features=SiteFeature.AI,
    )


class SanitizeArticlePlainTextTests(SimpleTestCase):
    """Markup must never survive into article appends."""

    def test_strips_html_tags(self) -> None:
        self.assertEqual(sanitize_article_plain_text("<p>Hello <b>world</b></p>"), "Hello world")

    def test_strips_entity_encoded_and_double_encoded_html(self) -> None:
        cleaned = sanitize_article_plain_text(
            "&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;Built in 1920."
        )
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertNotIn("alert", cleaned)
        self.assertIn("Built in 1920.", cleaned)

    def test_strips_script_and_keeps_text(self) -> None:
        text = sanitize_article_plain_text("<script>alert(1)</script>Built in 1920.")
        self.assertNotIn("<script", text)
        self.assertNotIn("alert", text)
        self.assertIn("Built in 1920.", text)

    def test_strips_markdown_links_and_headings(self) -> None:
        raw = "## History\n\nSee [the mill](https://evil.example) and ![x](https://evil.example/x.png)"
        cleaned = sanitize_article_plain_text(raw)
        self.assertNotIn("##", cleaned)
        self.assertNotIn("http", cleaned)
        self.assertNotIn("![", cleaned)
        self.assertIn("History", cleaned)
        self.assertIn("the mill", cleaned)

    def test_strips_code_fences_and_emphasis(self) -> None:
        cleaned = sanitize_article_plain_text("```\nignore\n```\n**Bold** and *italic*")
        self.assertNotIn("```", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertIn("Bold", cleaned)
        self.assertIn("italic", cleaned)

    def test_strips_strikethrough_and_inline_code(self) -> None:
        cleaned = sanitize_article_plain_text("~~wrong~~ correct, per `the plaque`.")
        self.assertNotIn("~~", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertIn("wrong", cleaned)
        self.assertIn("the plaque", cleaned)

    def test_preserves_non_markdown_uses_of_emphasis_characters(self) -> None:
        # Stray *, _, ~, ` characters that aren't paired markdown markers must
        # survive - only genuine emphasis syntax should be stripped.
        self.assertEqual(
            sanitize_article_plain_text("The reactor operated at ~500 degrees."),
            "The reactor operated at ~500 degrees.",
        )
        self.assertEqual(
            sanitize_article_plain_text("Known locally as Foo_Bar Mill."),
            "Known locally as Foo_Bar Mill.",
        )
        self.assertEqual(
            sanitize_article_plain_text("Production reached 5*3=15 tons per day."),
            "Production reached 5*3=15 tons per day.",
        )

    def test_empty_and_sentinel_values(self) -> None:
        self.assertEqual(sanitize_article_plain_text(""), "")
        self.assertEqual(sanitize_article_plain_text(None), "")
        self.assertEqual(sanitize_article_plain_text("NONE"), "")
        self.assertEqual(sanitize_article_plain_text("null"), "")

    def test_preserves_paragraph_breaks(self) -> None:
        cleaned = sanitize_article_plain_text("First paragraph.\n\nSecond paragraph.")
        self.assertEqual(cleaned, "First paragraph.\n\nSecond paragraph.")

    @given(st.text(max_size=500))
    @hypothesis_settings(max_examples=40, deadline=None)
    def test_sanitize_never_raises_and_bounds_length(self, raw: str) -> None:
        cleaned = sanitize_article_plain_text(raw)
        self.assertIsInstance(cleaned, str)
        self.assertLessEqual(len(cleaned), 8_000)
        self.assertNotIn("<script", cleaned.lower())
        self.assertNotIn("</", cleaned)


class ClassifyArticleTextTests(SimpleTestCase):
    """Fail-closed safety classifier token contract (no DB — gateway is mocked)."""

    def _classify(self, answer_token: str | None) -> ArticleSafetyVerdict:
        with patch(
            "urbanlens.dashboard.services.ai.factory.get_gateway",
            return_value=_FakeGateway(answer_token),
        ):
            return classify_article_text("The mill opened in 1920.", place_name="Old Mill", profile=None)

    def test_approve_token_approves(self) -> None:
        verdict = self._classify("APPROVE")
        self.assertTrue(verdict.approved)
        self.assertIsNone(verdict.reason)

    def test_reject_safety_token(self) -> None:
        verdict = self._classify("REJECT_SAFETY")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "safety")

    def test_reject_inappropriate_token(self) -> None:
        verdict = self._classify("REJECT_INAPPROPRIATE")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "inappropriate")

    def test_reject_off_topic_token(self) -> None:
        verdict = self._classify("REJECT_OFF_TOPIC")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "off_topic")

    def test_unparseable_fails_closed(self) -> None:
        verdict = self._classify("LOOKS_FINE_TO_ME")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "unparseable")

    def test_none_response_fails_closed(self) -> None:
        verdict = self._classify(None)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "ai_unavailable")

    def test_gateway_unavailable_fails_closed(self) -> None:
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=None):
            verdict = classify_article_text("text", place_name="X", profile=None)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "ai_unavailable")

    def test_proposed_text_is_wrapped_as_user_data(self) -> None:
        gateway = _FakeGateway("APPROVE")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            classify_article_text("Built in 1910.", place_name="Mill", profile=None)
        self.assertIn("<USER_DATA>", gateway.prompts[0])
        self.assertIn("Built in 1910.", gateway.prompts[0])


class ExpandArticlesFromPageTests(TestCase):
    """End-to-end expansion with writing + safety gateways mocked."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, official_name="Old Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Old Mill", name_is_user_provided=True)
        self.extraction = LinkExtraction.objects.create(
            profile=self.profile,
            pin=self.pin,
            url="https://example.com/history",
        )
        _grant_ai_to_everyone()

    def _gateways(self, *, write: str | None, safety: str | None):
        write_gw = _FakeGateway(write)
        safety_gw = _FakeGateway(safety)

        def _factory(feature: str | None = None, profile=None, **kwargs):
            if feature == "article_expansion":
                return write_gw
            if feature == "article_safety":
                return safety_gw
            return write_gw

        return _factory, write_gw, safety_gw

    def test_approve_appends_to_pin_article(self) -> None:
        factory, _, _ = self._gateways(write="The mill spun cotton until 1955.", safety="APPROVE")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "Page says the mill spun cotton until 1955.")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "article_pin")
        self.assertTrue(rows[0]["applied"])
        article = get_article(pin=self.pin)
        assert article is not None
        self.assertIn("cotton until 1955", article.content)
        revision = ArticleRevision.objects.filter(article=article).latest("created")
        self.assertEqual(revision.edit_summary, EDIT_SUMMARY_EXPANDED_FROM_LINK)
        self.assertEqual(revision.editor_id, self.profile.pk)

    def test_approve_appends_to_pin_and_wiki_when_wiki_exists(self) -> None:
        wiki = baker.make(Wiki, location=self.location, created_by=self.profile)
        save_article(editor=self.profile, content="Existing wiki intro.", wiki=wiki)
        factory, _, _ = self._gateways(write="New fact from the page.", safety="APPROVE")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "Useful page text.")
        keys = {row["key"] for row in rows}
        self.assertEqual(keys, {"article_pin", "article_wiki"})
        self.assertTrue(all(row["applied"] for row in rows))
        pin_article = get_article(pin=self.pin)
        wiki_article = get_article(wiki=wiki)
        self.assertIsNotNone(pin_article)
        self.assertIsNotNone(wiki_article)
        self.assertIn("New fact from the page.", pin_article.content)
        self.assertIn("Existing wiki intro.", wiki_article.content)
        self.assertIn("New fact from the page.", wiki_article.content)

    def test_safety_reject_applies_neither_article(self) -> None:
        wiki = baker.make(Wiki, location=self.location, created_by=self.profile)
        factory, _, _ = self._gateways(write="Climb the fence behind the loading dock.", safety="REJECT_SAFETY")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "How to get in…")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row["applied"] for row in rows))
        self.assertTrue(all("Safety review rejected" in row["note"] for row in rows))
        self.assertIsNone(get_article(pin=self.pin))
        self.assertIsNone(get_article(wiki=wiki))

    def test_empty_writing_skips_without_safety_call(self) -> None:
        factory, write_gw, safety_gw = self._gateways(write="", safety="APPROVE")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "Page text.")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["applied"])
        self.assertIn("Nothing new", rows[0]["note"])
        self.assertGreaterEqual(len(write_gw.prompts), 1)
        self.assertEqual(safety_gw.prompts, [])

    def test_feature_flags_off_returns_empty_and_skips_ai(self) -> None:
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(ai_article_expansion_enabled=False)
        called = MagicMock()
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=called):
            rows = expand_articles_from_page(self.extraction, "Page text.")
        self.assertEqual(rows, [])
        called.assert_not_called()

    def test_safety_flag_off_returns_empty(self) -> None:
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(ai_article_safety_enabled=False)
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway") as mock_gw:
            rows = expand_articles_from_page(self.extraction, "Page text.")
        self.assertEqual(rows, [])
        mock_gw.assert_not_called()

    def test_html_in_writing_output_is_stripped_before_save(self) -> None:
        factory, _, _ = self._gateways(
            write="<p>Built in <script>x</script>1910 by the Smith Co.</p>",
            safety="APPROVE",
        )
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "Page.")
        self.assertTrue(rows[0]["applied"])
        article = get_article(pin=self.pin)
        assert article is not None
        self.assertNotIn("<", article.content)
        self.assertNotIn("script", article.content)
        self.assertIn("Built in", article.content)
        self.assertIn("1910", article.content)

    def test_already_present_text_is_not_reapplied(self) -> None:
        save_article(editor=self.profile, content="The mill spun cotton until 1955.", pin=self.pin)
        factory, _, _ = self._gateways(write="The mill spun cotton until 1955.", safety="APPROVE")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "Page.")
        self.assertFalse(rows[0]["applied"])
        self.assertIn("Already present", rows[0]["note"])
        self.assertEqual(ArticleRevision.objects.filter(article__pin=self.pin).count(), 1)

    def test_length_cap_skips_when_article_is_full(self) -> None:
        huge = "x" * (MAX_ARTICLE_LENGTH - 10)
        Article.objects.create(pin=self.pin, content=huge, content_html="", toc=[])
        factory, _, _ = self._gateways(write="A new sentence that will not fit.", safety="APPROVE")
        with patch("urbanlens.dashboard.services.ai.factory.get_gateway", side_effect=factory):
            rows = expand_articles_from_page(self.extraction, "Page.")
        self.assertFalse(rows[0]["applied"])
        self.assertIn("length limit", rows[0]["note"])


class RunExtractionArticleHookTests(TestCase):
    """run_extraction merges article rows and keeps EMPTY when only skips land."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)
        self.extraction = LinkExtraction.objects.create(
            profile=self.profile,
            pin=self.pin,
            url="https://example.com/history",
        )
        _grant_ai_to_everyone()

    def test_applied_article_alone_marks_success(self) -> None:
        article_rows = [
            {"key": "article_pin", "label": "Pin article", "value": "New text", "applied": True, "note": "Appended"},
        ]
        with (
            patch("urbanlens.dashboard.services.ai.link_extraction.fetch_page_text", return_value="page"),
            patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=_FakeGateway("{}")),
            patch(
                "urbanlens.dashboard.services.ai.article_expansion.expand_articles_from_page",
                return_value=article_rows,
            ),
        ):
            run_extraction(self.extraction)
        self.extraction.refresh_from_db()
        self.assertEqual(self.extraction.status, LinkExtractionStatus.SUCCESS)
        self.assertEqual(self.extraction.results_rows[-1]["key"], "article_pin")

    def test_rejected_article_alone_stays_empty(self) -> None:
        article_rows = [
            {
                "key": "article_pin",
                "label": "Pin article",
                "value": "bad",
                "applied": False,
                "note": "Safety review rejected (safety)",
            },
        ]
        with (
            patch("urbanlens.dashboard.services.ai.link_extraction.fetch_page_text", return_value="page"),
            patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=_FakeGateway("{}")),
            patch(
                "urbanlens.dashboard.services.ai.article_expansion.expand_articles_from_page",
                return_value=article_rows,
            ),
        ):
            run_extraction(self.extraction)
        self.extraction.refresh_from_db()
        self.assertEqual(self.extraction.status, LinkExtractionStatus.EMPTY)
