"""Tests for services.trivia.generation - AI question generation from wiki articles."""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.trivia.classifier import ClassifierVerdict
from urbanlens.dashboard.services.trivia.generation import (
    MIN_DESCRIPTION_LENGTH,
    generate_questions_for_wiki,
    sweep_wikis_for_generation,
)

_LONG_DESCRIPTION = "This building has a long and storied history. " * 20
assert len(_LONG_DESCRIPTION) >= MIN_DESCRIPTION_LENGTH


class _FakeGateway:
    def __init__(self, pairs: list[str]):
        self._pairs = pairs
        self.model = "fake-model"

    def send_prompt_list(self, prompt: str, *, max_results=None, **kwargs) -> list[str]:
        return self._pairs[:max_results] if max_results else self._pairs


def _make_wiki(description: str | None = _LONG_DESCRIPTION) -> Wiki:
    location = baker.make(Location)
    return baker.make(Wiki, location=location, description=description)


class GenerateQuestionsForWikiTests(TestCase):
    def test_skips_a_wiki_with_no_description(self) -> None:
        wiki = _make_wiki(description=None)
        self.assertEqual(generate_questions_for_wiki(wiki), [])

    def test_skips_a_wiki_with_a_short_description(self) -> None:
        wiki = _make_wiki(description="Too short.")
        self.assertEqual(generate_questions_for_wiki(wiki), [])

    def test_skips_a_location_already_ai_generated(self) -> None:
        wiki = _make_wiki()
        baker.make(TriviaQuestion, location=wiki.location, source=TriviaQuestionSource.AI_GENERATED, answer="1937")
        with patch("urbanlens.dashboard.services.trivia.generation.get_gateway") as mock_get_gateway:
            self.assertEqual(generate_questions_for_wiki(wiki), [])
        mock_get_gateway.assert_not_called()

    def test_skips_when_ai_unavailable(self) -> None:
        wiki = _make_wiki()
        with patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=None):
            self.assertEqual(generate_questions_for_wiki(wiki), [])

    def test_approved_pair_is_persisted(self) -> None:
        wiki = _make_wiki()
        gateway = _FakeGateway(["What year was it built?|||1937"])
        with (
            patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=gateway),
            patch(
                "urbanlens.dashboard.services.trivia.generation.classify_trivia_question",
                return_value=ClassifierVerdict(approved=True),
            ),
        ):
            created = generate_questions_for_wiki(wiki)

        self.assertEqual(len(created), 1)
        question = created[0]
        self.assertEqual(question.source, TriviaQuestionSource.AI_GENERATED)
        self.assertEqual(question.prompt, "What year was it built?")
        self.assertEqual(question.answer, "1937")

    def test_rejected_pair_is_never_persisted(self) -> None:
        wiki = _make_wiki()
        gateway = _FakeGateway(["What was X in the year somebody did Y?|||1937"])
        with (
            patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=gateway),
            patch(
                "urbanlens.dashboard.services.trivia.generation.classify_trivia_question",
                return_value=ClassifierVerdict(approved=False, reason="person"),
            ),
        ):
            created = generate_questions_for_wiki(wiki)

        self.assertEqual(created, [])
        self.assertFalse(TriviaQuestion.objects.filter(location=wiki.location).exists())

    def test_malformed_pair_with_no_separator_is_discarded(self) -> None:
        wiki = _make_wiki()
        gateway = _FakeGateway(["this has no separator at all"])
        with patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=gateway):
            created = generate_questions_for_wiki(wiki)
        self.assertEqual(created, [])

    def test_mixed_approve_and_reject_only_persists_the_approved_one(self) -> None:
        wiki = _make_wiki()
        gateway = _FakeGateway(["Good question|||42", "Bad question|||99"])
        verdicts = [ClassifierVerdict(approved=True), ClassifierVerdict(approved=False, reason="bullying")]
        with (
            patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=gateway),
            patch("urbanlens.dashboard.services.trivia.generation.classify_trivia_question", side_effect=verdicts),
        ):
            created = generate_questions_for_wiki(wiki)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].prompt, "Good question")


class SweepWikisForGenerationTests(TestCase):
    def test_sweeps_eligible_wikis_and_reports_counts(self) -> None:
        _make_wiki()
        _make_wiki(description="too short")
        gateway = _FakeGateway(["What year was it built?|||1937"])
        with (
            patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=gateway),
            patch(
                "urbanlens.dashboard.services.trivia.generation.classify_trivia_question",
                return_value=ClassifierVerdict(approved=True),
            ),
        ):
            summary = sweep_wikis_for_generation(batch_size=10)

        self.assertEqual(summary["wikis_considered"], 1)
        self.assertEqual(summary["questions_created"], 1)

    def test_skips_wikis_already_ai_generated(self) -> None:
        wiki = _make_wiki()
        baker.make(TriviaQuestion, location=wiki.location, source=TriviaQuestionSource.AI_GENERATED, answer="1937")
        with patch("urbanlens.dashboard.services.trivia.generation.get_gateway") as mock_get_gateway:
            summary = sweep_wikis_for_generation(batch_size=10)
        self.assertEqual(summary["wikis_considered"], 0)
        mock_get_gateway.assert_not_called()

    def test_respects_batch_size(self) -> None:
        for _ in range(5):
            _make_wiki()
        with patch("urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=None):
            summary = sweep_wikis_for_generation(batch_size=2)
        self.assertEqual(summary["wikis_considered"], 2)
