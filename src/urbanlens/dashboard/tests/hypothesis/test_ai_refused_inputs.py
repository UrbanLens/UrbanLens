"""An AI call with nothing to work on is refused before it reserves a slot in the budget."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.location.model import Location


class _NoSlotCase(TestCase):
    """Fails the test if any AI call reserves a slot or reaches a model."""

    def setUp(self) -> None:
        super().setUp()
        self.gateway = mock.Mock(model="test-model", cost=0)
        reserve = mock.patch(
            "urbanlens.dashboard.services.core.rate_limiter._reserve_call",
            side_effect=AssertionError("reserved a slot"),
        )
        reserve.start()
        self.addCleanup(reserve.stop)

    def assertRefusedUnder(self, service: str) -> None:
        self.assertTrue(ApiCallLog.objects.filter(service=service, was_rejected_input=True).exists())
        self.gateway.send_prompt.assert_not_called()


class VisionTests(_NoSlotCase):
    def test_an_empty_image_is_neither_described_nor_classified(self) -> None:
        from urbanlens.dashboard.services.ai import vision

        self.assertEqual(vision.describe_photo_keywords(b""), [])
        self.assertEqual(vision.classify_photo(b""), [])
        self.assertRefusedUnder(vision.SERVICE_AI_PHOTO_KEYWORDS)
        self.assertRefusedUnder(vision.SERVICE_PHOTO_CLASSIFIER)


class TriviaTests(_NoSlotCase):
    def test_a_blank_question_or_answer_is_rejected_without_asking(self) -> None:
        from urbanlens.dashboard.services.trivia import classifier

        location = baker.make(Location)
        with mock.patch.object(classifier, "get_gateway", return_value=self.gateway):
            for prompt, answer in (("  ", "1912"), ("When was it built?", "")):
                with self.subTest(prompt=prompt, answer=answer):
                    verdict = classifier.classify_trivia_question(prompt, answer, location)
                    self.assertFalse(verdict.approved)
                    self.assertEqual(verdict.reason, "empty")
        self.assertRefusedUnder("trivia_moderation")

    def test_a_blank_player_answer_is_no_match_without_asking(self) -> None:
        from urbanlens.dashboard.services.trivia import answer_check

        profile = baker.make(User).profile
        with (
            mock.patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=True),
            mock.patch.object(answer_check, "get_gateway", return_value=self.gateway),
        ):
            self.assertFalse(answer_check.is_answer_equivalent("   ", "1912", profile=profile))
        self.assertRefusedUnder("trivia_answer_check")

    def test_a_blank_fact_drafts_nothing_without_asking(self) -> None:
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        with mock.patch.object(wiki_incorporation, "get_gateway", return_value=self.gateway):
            self.assertEqual(
                wiki_incorporation._draft_paragraph(place_name="Asylum", prompt="", answer="1912", existing_article=""),
                "",
            )
        self.assertRefusedUnder("trivia_wiki_incorporation")


class ArticleTests(_NoSlotCase):
    def test_a_blank_page_drafts_nothing_without_asking(self) -> None:
        from urbanlens.dashboard.services.ai import article_expansion

        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=self.gateway):
            raw = article_expansion._draft_new_paragraphs(
                place_name="Asylum", page_text=" \n ", pin_article="", wiki_article=None, wiki=None, profile=None
            )
        self.assertEqual(raw, "")
        self.assertRefusedUnder("article_expansion")

    def test_blank_text_is_not_approved_without_asking(self) -> None:
        from urbanlens.dashboard.services.ai import article_safety

        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=self.gateway):
            verdict = article_safety.classify_article_text("  ", place_name="Asylum")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.reason, "empty")
        self.assertRefusedUnder("article_safety")


class AssistantTests(_NoSlotCase):
    def test_a_blank_message_is_answered_without_asking_the_model(self) -> None:
        from urbanlens.dashboard.services.ai import assistant

        profile = baker.make(User).profile
        with mock.patch.object(assistant, "get_gateway", return_value=self.gateway):
            turn = assistant.run_assistant_turn(profile, [], "   ")
        self.assertTrue(turn.reply)
        self.gateway.send_with_tools.assert_not_called()
        self.assertTrue(ApiCallLog.objects.filter(service="assistant", was_rejected_input=True).exists())
        self.assertFalse(ApiCallLog.objects.filter(service="assistant", was_rejected_input=False).exists())
