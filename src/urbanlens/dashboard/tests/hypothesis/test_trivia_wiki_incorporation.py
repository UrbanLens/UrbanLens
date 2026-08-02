"""Tests for services.trivia.wiki_incorporation - folding well-upvoted user trivia into wikis."""

from __future__ import annotations

from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus, TriviaQuestionVote, TriviaQuestionVoteKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.ai.article_safety import ArticleSafetyVerdict
from urbanlens.dashboard.services.wiki.articles import get_article, save_article
from urbanlens.dashboard.services.trivia.wiki_incorporation import (
    EDIT_SUMMARY_INCORPORATED_TRIVIA,
    WIKI_INCORPORATION_SCORE_THRESHOLD,
    incorporate_question_into_wiki,
    sweep_questions_for_wiki_incorporation,
)

_coordinate_counter = count()


class _FakeGateway:
    def __init__(self, answer: str | None):
        self._answer = answer
        self.model = "fake-model"
        self.prompts: list[str] = []

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        self.prompts.append(prompt)
        return self._answer

    @property
    def cost(self) -> Decimal:
        return Decimal("0.001")


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, official_name="Old Armory", latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_wiki(location: Location) -> Wiki:
    return baker.make(Wiki, location=location, created_by=_make_profile())


def _make_question(location: Location, **kwargs) -> TriviaQuestion:
    defaults = {
        "location": location,
        "source": TriviaQuestionSource.USER_SUBMITTED,
        "status": TriviaQuestionStatus.APPROVED,
        "prompt": "What year was it built?",
        "answer": "1937",
    }
    defaults.update(kwargs)
    return baker.make(TriviaQuestion, **defaults)


def _upvote(question: TriviaQuestion, count_: int) -> None:
    """Give ``question`` enough upvotes to cross WIKI_INCORPORATION_SCORE_THRESHOLD (1.0 each)."""
    for _ in range(count_):
        baker.make(TriviaQuestionVote, question=question, profile=_make_profile(), kind=TriviaQuestionVoteKind.UPVOTE)


class IncorporateQuestionIntoWikiTests(TestCase):
    def setUp(self) -> None:
        self.location = _make_location()
        self.wiki = _make_wiki(self.location)
        self.question = _make_question(self.location)
        _upvote(self.question, 5)  # exactly meets the >= threshold

    def _run(self, *, write: str | None, safety_approved: bool = True, safety_reason: str | None = None):
        gateway = _FakeGateway(write)
        with (
            patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway", return_value=gateway),
            patch(
                "urbanlens.dashboard.services.trivia.wiki_incorporation.classify_article_text",
                return_value=ArticleSafetyVerdict(approved=safety_approved, reason=safety_reason),
            ),
        ):
            return incorporate_question_into_wiki(self.question), gateway

    def test_below_threshold_never_calls_the_gateway(self) -> None:
        question = _make_question(self.location, prompt="Q2", answer="A2")
        _upvote(question, 4)  # 4.0 < WIKI_INCORPORATION_SCORE_THRESHOLD
        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway") as mock_get_gateway:
            result = incorporate_question_into_wiki(question)
        self.assertFalse(result)
        mock_get_gateway.assert_not_called()
        question.refresh_from_db()
        self.assertIsNone(question.wiki_incorporated_at)

    def test_already_incorporated_is_a_no_op(self) -> None:
        self.question.wiki_incorporated_at = timezone.now()
        self.question.save(update_fields=["wiki_incorporated_at"])
        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway") as mock_get_gateway:
            result = incorporate_question_into_wiki(self.question)
        self.assertFalse(result)
        mock_get_gateway.assert_not_called()

    def test_ai_generated_question_is_never_considered(self) -> None:
        question = _make_question(self.location, source=TriviaQuestionSource.AI_GENERATED, prompt="Q3", answer="A3")
        _upvote(question, 10)
        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway") as mock_get_gateway:
            result = incorporate_question_into_wiki(question)
        self.assertFalse(result)
        mock_get_gateway.assert_not_called()

    def test_pending_review_question_is_never_considered(self) -> None:
        question = _make_question(self.location, status=TriviaQuestionStatus.PENDING_REVIEW, prompt="Q4", answer="A4")
        _upvote(question, 10)
        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway") as mock_get_gateway:
            result = incorporate_question_into_wiki(question)
        self.assertFalse(result)
        mock_get_gateway.assert_not_called()

    def test_location_with_no_wiki_is_skipped(self) -> None:
        location = _make_location()
        question = _make_question(location, prompt="Q5", answer="A5")
        _upvote(question, 10)
        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway") as mock_get_gateway:
            result = incorporate_question_into_wiki(question)
        self.assertFalse(result)
        mock_get_gateway.assert_not_called()

    def test_ai_unavailable_leaves_wiki_incorporated_at_unset(self) -> None:
        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway", return_value=None):
            result = incorporate_question_into_wiki(self.question)
        self.assertFalse(result)
        self.question.refresh_from_db()
        self.assertIsNone(self.question.wiki_incorporated_at)

    def test_writing_gateway_exception_is_treated_as_unavailable(self) -> None:
        class _RaisingGateway(_FakeGateway):
            def send_prompt(self, prompt: str, **kwargs) -> str | None:
                raise RuntimeError("boom")

        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway", return_value=_RaisingGateway("x")):
            result = incorporate_question_into_wiki(self.question)
        self.assertFalse(result)
        self.question.refresh_from_db()
        self.assertIsNone(self.question.wiki_incorporated_at)

    def test_empty_draft_marks_processed_without_appending(self) -> None:
        result, _gateway = self._run(write="")
        self.assertFalse(result)
        self.question.refresh_from_db()
        self.assertIsNotNone(self.question.wiki_incorporated_at)
        self.assertIsNone(get_article(wiki=self.wiki))

    def test_safety_rejected_marks_processed_without_appending(self) -> None:
        result, gateway = self._run(write="The armory was built for the state militia.", safety_approved=False, safety_reason="off_topic")
        self.assertFalse(result)
        self.assertEqual(len(gateway.prompts), 1)
        self.question.refresh_from_db()
        self.assertIsNotNone(self.question.wiki_incorporated_at)
        self.assertIsNone(get_article(wiki=self.wiki))

    def test_approved_draft_appends_and_marks_processed(self) -> None:
        result, _gateway = self._run(write="The armory was built in 1937 for the state militia.")
        self.assertTrue(result)
        self.question.refresh_from_db()
        self.assertIsNotNone(self.question.wiki_incorporated_at)
        article = get_article(wiki=self.wiki)
        assert article is not None
        self.assertIn("1937", article.content)
        revision = ArticleRevision.objects.filter(article=article).latest("created")
        self.assertEqual(revision.edit_summary, EDIT_SUMMARY_INCORPORATED_TRIVIA)
        self.assertIsNone(revision.editor_id)

    def test_appends_after_existing_wiki_content(self) -> None:
        save_article(editor=None, content="Existing intro paragraph.", wiki=self.wiki)
        result, _gateway = self._run(write="The armory was built in 1937.")
        self.assertTrue(result)
        article = get_article(wiki=self.wiki)
        assert article is not None
        self.assertIn("Existing intro paragraph.", article.content)
        self.assertIn("1937", article.content)

    def test_question_and_answer_are_wrapped_as_untrusted_data(self) -> None:
        _result, gateway = self._run(write="")
        self.assertIn("<USER_DATA>", gateway.prompts[0])
        self.assertIn(self.question.prompt, gateway.prompts[0])

    def test_exact_threshold_score_qualifies(self) -> None:
        self.assertEqual(WIKI_INCORPORATION_SCORE_THRESHOLD, 5.0)
        result, _gateway = self._run(write="The armory was built in 1937.")
        self.assertTrue(result)


class SweepQuestionsForWikiIncorporationTests(TestCase):
    def setUp(self) -> None:
        self.location = _make_location()
        self.wiki = _make_wiki(self.location)

    def test_counts_considered_and_incorporated(self) -> None:
        eligible = _make_question(self.location, prompt="Eligible", answer="Yes")
        _upvote(eligible, 5)
        ineligible = _make_question(self.location, prompt="Too new", answer="No")
        _upvote(ineligible, 1)

        with (
            patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway", return_value=_FakeGateway("A new fact about the armory.")),
            patch(
                "urbanlens.dashboard.services.trivia.wiki_incorporation.classify_article_text",
                return_value=ArticleSafetyVerdict(approved=True),
            ),
        ):
            summary = sweep_questions_for_wiki_incorporation(batch_size=10)

        self.assertEqual(summary["questions_considered"], 2)
        self.assertEqual(summary["questions_incorporated"], 1)
        eligible.refresh_from_db()
        ineligible.refresh_from_db()
        self.assertIsNotNone(eligible.wiki_incorporated_at)
        self.assertIsNone(ineligible.wiki_incorporated_at)

    def test_already_incorporated_question_is_excluded_from_candidates(self) -> None:
        question = _make_question(self.location)
        _upvote(question, 5)
        question.wiki_incorporated_at = timezone.now()
        question.save(update_fields=["wiki_incorporated_at"])

        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway") as mock_get_gateway:
            summary = sweep_questions_for_wiki_incorporation(batch_size=10)

        self.assertEqual(summary["questions_considered"], 0)
        mock_get_gateway.assert_not_called()

    def test_location_without_wiki_is_excluded_from_candidates(self) -> None:
        bare_location = _make_location()
        question = _make_question(bare_location)
        _upvote(question, 5)

        summary = sweep_questions_for_wiki_incorporation(batch_size=10)
        self.assertEqual(summary["questions_considered"], 0)

    def test_respects_batch_size(self) -> None:
        for i in range(5):
            question = _make_question(self.location, prompt=f"Q{i}", answer=f"A{i}")
            _upvote(question, 5)

        with patch("urbanlens.dashboard.services.trivia.wiki_incorporation.get_gateway", return_value=None):
            summary = sweep_questions_for_wiki_incorporation(batch_size=2)
        self.assertEqual(summary["questions_considered"], 2)
