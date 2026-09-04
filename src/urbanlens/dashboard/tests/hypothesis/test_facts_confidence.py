"""Tests for the Facts confidence-recomputation math (services.facts.confidence).

Pure math over primitives (weighted evidence, cluster totals), no DB -
mirrors ``test_consensus_trust.py``'s approach of hypothesis-testing the
underlying formulas directly rather than through the DB-touching
``recompute()`` entry point, whose end-to-end behavior is covered by
``test_facts_evidence.py``'s integration tests instead.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.facts.model import FactStatus
from urbanlens.dashboard.services.facts.confidence import (
    CONFIRM_THRESHOLD,
    _cluster_categorical,
    _decay,
    _WeightedEvidence,
    resolve_categorical,
)

_HYP = {"max_examples": 200, "deadline": None}

#: Realistic single-evidence-row weight range - source_reliability, submitter
#: trust, and decay are each in (0, 1], so one row's weight never exceeds 1.
_WEIGHTS = st.floats(min_value=0.01, max_value=1.0, allow_nan=False)


class DecayTests(SimpleTestCase):
    def test_zero_age_means_no_decay(self) -> None:
        self.assertAlmostEqual(_decay(0.0), 1.0)

    @given(younger=st.floats(min_value=0, max_value=5000), older_extra=st.floats(min_value=0.01, max_value=5000))
    @settings(**_HYP)
    def test_monotonically_decreasing_in_age(self, younger: float, older_extra: float) -> None:
        self.assertGreater(_decay(younger), _decay(younger + older_extra))

    @given(age_days=st.floats(min_value=0, max_value=100_000))
    @settings(**_HYP)
    def test_always_in_unit_interval(self, age_days: float) -> None:
        decay = _decay(age_days)
        self.assertGreater(decay, 0.0)
        self.assertLessEqual(decay, 1.0)


class ResolveCategoricalTests(SimpleTestCase):
    def test_no_evidence_is_unconfirmed(self) -> None:
        value, confidence, status = resolve_categorical([], 0.0, previous_value=None, previously_confirmed=False)
        self.assertIsNone(value)
        self.assertEqual(confidence, 0.0)
        self.assertEqual(status, FactStatus.UNCONFIRMED)

    @given(weight=_WEIGHTS)
    @settings(**_HYP)
    def test_confidence_always_in_unit_interval(self, weight: float) -> None:
        totals, total_weight = _cluster_categorical([_WeightedEvidence(value="a", weight=weight)])
        _value, confidence, _status = resolve_categorical(
            totals, total_weight, previous_value=None, previously_confirmed=False
        )
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 1.0)

    @given(weight=_WEIGHTS)
    @settings(**_HYP)
    def test_a_single_observation_never_reads_as_near_full_confidence(self, weight: float) -> None:
        """One piece of evidence must never look like certainty - see PRIOR_ALPHA/PRIOR_BETA."""
        totals, total_weight = _cluster_categorical([_WeightedEvidence(value="a", weight=weight)])
        _value, confidence, _status = resolve_categorical(
            totals, total_weight, previous_value=None, previously_confirmed=False
        )
        self.assertLess(confidence, 0.7)

    @given(
        base_weight=_WEIGHTS,
        other_fraction=st.floats(min_value=0.0, max_value=1.0),
        extra=st.floats(min_value=0.01, max_value=1.0),
    )
    @settings(**_HYP)
    def test_more_agreeing_evidence_for_the_leader_never_decreases_confidence(
        self, base_weight: float, other_fraction: float, extra: float
    ) -> None:
        # other_weight <= base_weight guarantees "a" leads both before and
        # after (it only gains weight), so this isolates the property from
        # cases where the *leader itself* changes between the two calls.
        other_weight = base_weight * other_fraction
        before = [_WeightedEvidence(value="a", weight=base_weight), _WeightedEvidence(value="b", weight=other_weight)]
        after = [*before, _WeightedEvidence(value="a", weight=extra)]

        before_totals, before_total_weight = _cluster_categorical(before)
        after_totals, after_total_weight = _cluster_categorical(after)
        _value, before_confidence, _status = resolve_categorical(
            before_totals, before_total_weight, previous_value=None, previously_confirmed=False
        )
        _value, after_confidence, _status = resolve_categorical(
            after_totals, after_total_weight, previous_value=None, previously_confirmed=False
        )
        self.assertGreaterEqual(after_confidence, before_confidence)

    def test_a_near_tie_is_contested(self) -> None:
        totals, total_weight = _cluster_categorical(
            [_WeightedEvidence(value="a", weight=0.51), _WeightedEvidence(value="b", weight=0.49)]
        )
        _value, _confidence, status = resolve_categorical(
            totals, total_weight, previous_value=None, previously_confirmed=False
        )
        self.assertEqual(status, FactStatus.CONTESTED)

    def test_a_clear_majority_is_not_contested(self) -> None:
        evidence = [_WeightedEvidence(value="a", weight=1.0) for _ in range(3)] + [
            _WeightedEvidence(value="b", weight=0.2)
        ]
        totals, total_weight = _cluster_categorical(evidence)
        value, _confidence, status = resolve_categorical(
            totals, total_weight, previous_value=None, previously_confirmed=False
        )
        self.assertEqual(value, "a")
        self.assertNotEqual(status, FactStatus.CONTESTED)

    def test_a_confirmed_value_survives_a_weak_challenger(self) -> None:
        """A CONFIRMED value must not flip to a new leading cluster that itself doesn't clear CONFIRM_THRESHOLD."""
        # "a" was confirmed on 5 units of weight; "b" now nominally leads
        # with 6, but 6/(6+5+4) is still well under CONFIRM_THRESHOLD.
        totals, total_weight = _cluster_categorical(
            [_WeightedEvidence(value="a", weight=1.0) for _ in range(5)]
            + [_WeightedEvidence(value="b", weight=1.0) for _ in range(6)],
        )
        value, confidence, status = resolve_categorical(
            totals, total_weight, previous_value="a", previously_confirmed=True
        )
        self.assertEqual(value, "a")
        self.assertLess(confidence, CONFIRM_THRESHOLD)
        self.assertNotEqual(status, FactStatus.CONFIRMED)

    def test_a_confirmed_value_yields_to_an_overwhelming_challenger(self) -> None:
        """A CONFIRMED value does change once the new leading cluster itself clears CONFIRM_THRESHOLD."""
        totals, total_weight = _cluster_categorical(
            [_WeightedEvidence(value="a", weight=1.0)] + [_WeightedEvidence(value="b", weight=1.0) for _ in range(20)],
        )
        value, confidence, status = resolve_categorical(
            totals, total_weight, previous_value="a", previously_confirmed=True
        )
        self.assertEqual(value, "b")
        self.assertGreaterEqual(confidence, CONFIRM_THRESHOLD)
        self.assertEqual(status, FactStatus.CONFIRMED)
