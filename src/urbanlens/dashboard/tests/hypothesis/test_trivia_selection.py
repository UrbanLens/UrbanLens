"""Tests for services.trivia.selection - difficulty weighting and weight_overrides."""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource
from urbanlens.dashboard.services.trivia.selection import pick_next_question


def _make_question() -> TriviaQuestion:
    return baker.make(
        TriviaQuestion, location=baker.make(Location), source=TriviaQuestionSource.DETERMINISTIC, answer="1937"
    )


class PickNextQuestionTests(TestCase):
    def test_empty_pool_returns_none(self) -> None:
        self.assertIsNone(pick_next_question([], difficulty=0.5))

    def test_single_candidate_is_always_picked(self) -> None:
        question = _make_question()
        self.assertEqual(pick_next_question([question], difficulty=0.5), question)

    def test_weight_overrides_are_applied_on_top_of_difficulty_weight(self) -> None:
        """A near-zero override should make random.choices see a near-zero weight for that question."""
        normal = _make_question()
        rare = _make_question()

        with patch("random.choices", return_value=[normal]) as mock_choices:
            pick_next_question([normal, rare], difficulty=0.5, weight_overrides={rare.pk: 0.03})

        call_args = mock_choices.call_args
        pool_arg = call_args.args[0]
        weights_arg = call_args.kwargs["weights"]
        weights_by_pk = dict(zip((q.pk for q in pool_arg), weights_arg, strict=True))
        self.assertLess(weights_by_pk[rare.pk], weights_by_pk[normal.pk])
        # The override multiplies a Gaussian weight that's the same for both
        # (neither has rating history), so the ratio should match the override exactly.
        self.assertAlmostEqual(weights_by_pk[rare.pk] / weights_by_pk[normal.pk], 0.03, places=6)

    def test_no_override_leaves_equal_weight_candidates_equally_likely(self) -> None:
        a, b = _make_question(), _make_question()
        with patch("random.choices", return_value=[a]) as mock_choices:
            pick_next_question([a, b], difficulty=0.5)
        weights_arg = mock_choices.call_args.kwargs["weights"]
        self.assertAlmostEqual(weights_arg[0], weights_arg[1], places=6)
