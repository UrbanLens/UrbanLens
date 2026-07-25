"""Tests for TriviaQuestion.normalize_answer / answer_normalized (models.trivia.model)."""

from __future__ import annotations

from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource


class NormalizeAnswerPropertyTests(SimpleTestCase):
    """Pure-function properties - no DB needed, matching CLAUDE.md's @given/self.client guidance."""

    @given(st.text(min_size=1, max_size=100))
    def test_is_idempotent(self, text: str) -> None:
        once = TriviaQuestion.normalize_answer(text)
        twice = TriviaQuestion.normalize_answer(once)
        self.assertEqual(once, twice)

    @given(st.text(min_size=1, max_size=100))
    def test_is_always_lowercase_alphanumeric_or_empty(self, text: str) -> None:
        normalized = TriviaQuestion.normalize_answer(text)
        self.assertTrue(all(char.isalnum() and not char.isupper() for char in normalized))

    def test_matches_regardless_of_case_and_punctuation(self) -> None:
        self.assertEqual(TriviaQuestion.normalize_answer("1937"), TriviaQuestion.normalize_answer("1937"))
        self.assertEqual(TriviaQuestion.normalize_answer("The Armory"), TriviaQuestion.normalize_answer("the-armory!"))
        self.assertEqual(TriviaQuestion.normalize_answer("St. Mary's Church"), TriviaQuestion.normalize_answer("st marys church"))

    def test_different_answers_do_not_collide(self) -> None:
        self.assertNotEqual(TriviaQuestion.normalize_answer("1937"), TriviaQuestion.normalize_answer("1938"))


class TriviaQuestionSaveTests(TestCase):
    def test_answer_normalized_is_computed_on_save(self) -> None:
        question = baker.make(
            TriviaQuestion,
            location=baker.make(Location),
            source=TriviaQuestionSource.DETERMINISTIC,
            answer="The Armory!",
        )
        self.assertEqual(question.answer_normalized, "thearmory")

    def test_answer_normalized_updates_when_answer_changes(self) -> None:
        question = baker.make(
            TriviaQuestion,
            location=baker.make(Location),
            source=TriviaQuestionSource.DETERMINISTIC,
            answer="1937",
        )
        question.answer = "1938"
        question.save()
        self.assertEqual(question.answer_normalized, "1938")
