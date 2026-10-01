"""One piece of evidence must never read as certainty."""

from __future__ import annotations

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.facts.confidence import (
    CONFIRM_THRESHOLD,
    _aggregate_number,
    _confidence_for_weight,
    _WeightedEvidence,
)


class FactConfidencePriorTests(SimpleTestCase):
    def test_one_unanimous_submission_is_not_certainty(self) -> None:
        self.assertLess(_confidence_for_weight(1.0, 1.0), 1.0)

    def test_one_unanimous_submission_cannot_confirm_a_fact(self) -> None:
        """The point of the prior: one person does not settle a question."""
        self.assertLess(_confidence_for_weight(1.0, 1.0), CONFIRM_THRESHOLD)

    def test_overwhelming_agreement_approaches_but_never_reaches_certainty(self) -> None:
        self.assertGreater(_confidence_for_weight(1000.0, 1000.0), 0.9)
        self.assertLess(_confidence_for_weight(1000.0, 1000.0), 1.0)

    def test_a_single_number_observation_scores_low(self) -> None:
        """A separate guard - the count factor, not the prior."""
        _value, confidence = _aggregate_number([_WeightedEvidence(value=10.0, weight=1.0)])

        self.assertLessEqual(confidence, 0.25)

    def test_more_agreement_never_lowers_confidence(self) -> None:
        weaker = _confidence_for_weight(2.0, 2.0)
        stronger = _confidence_for_weight(20.0, 20.0)

        self.assertGreaterEqual(stronger, weaker)

    def test_disagreement_lowers_confidence(self) -> None:
        unanimous = _confidence_for_weight(10.0, 10.0)
        contested = _confidence_for_weight(6.0, 10.0)

        self.assertLess(contested, unanimous)

    @given(
        weight=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
        extra=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=60, deadline=None)
    def test_confidence_stays_a_probability(self, weight: float, extra: float) -> None:
        total = weight + extra
        if total <= 0:
            return
        confidence = _confidence_for_weight(weight, total)

        self.assertGreaterEqual(confidence, 0.0)
        self.assertLess(confidence, 1.0, "no amount of evidence should read as absolute certainty")
