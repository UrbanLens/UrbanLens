"""Tests for the Consensus trust-scoring formulas (services.consensus.trust).

Pure math over a ConsensusProfile-shaped object, no DB - see
``services.consensus.trust``'s module docstring for the Beta-Bernoulli-
with-forgetting rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.consensus.trust import (
    CHECK_PROBABILITY_MAX,
    CHECK_PROBABILITY_MIN,
    TRUST_DECAY_GAMMA,
    check_probability,
)

_HYP = {"max_examples": 200, "deadline": None}


@dataclass
class _FakeConsensusProfile:
    """Stand-in for ConsensusProfile - only needs the fields trust.py actually reads."""

    trust_alpha: float
    trust_beta: float

    @property
    def trust_score(self) -> float:
        return self.trust_alpha / (self.trust_alpha + self.trust_beta)


def _apply_check(profile: _FakeConsensusProfile, *, correct: bool) -> _FakeConsensusProfile:
    """Mirrors trust.record_check_result's update math, without touching the DB."""
    base_alpha, base_beta = 2.0, 2.0
    alpha = TRUST_DECAY_GAMMA * (profile.trust_alpha - base_alpha) + base_alpha
    beta = TRUST_DECAY_GAMMA * (profile.trust_beta - base_beta) + base_beta
    if correct:
        alpha += 1.0
    else:
        beta += 1.0
    return _FakeConsensusProfile(trust_alpha=alpha, trust_beta=beta)


class TrustScoreBoundsTests(SimpleTestCase):
    @given(alpha=st.floats(min_value=0.01, max_value=1000), beta=st.floats(min_value=0.01, max_value=1000))
    @settings(**_HYP)
    def test_trust_score_always_in_unit_interval(self, alpha: float, beta: float) -> None:
        profile = _FakeConsensusProfile(trust_alpha=alpha, trust_beta=beta)
        self.assertGreater(profile.trust_score, 0.0)
        self.assertLess(profile.trust_score, 1.0)

    def test_default_prior_is_centered(self) -> None:
        profile = _FakeConsensusProfile(trust_alpha=2.0, trust_beta=2.0)
        self.assertAlmostEqual(profile.trust_score, 0.5)


class RecordCheckResultTests(SimpleTestCase):
    @given(alpha=st.floats(min_value=0.5, max_value=100), beta=st.floats(min_value=0.5, max_value=100))
    @settings(**_HYP)
    def test_a_correct_check_always_scores_higher_than_an_incorrect_one_would_have(
        self, alpha: float, beta: float
    ) -> None:
        """From the same starting profile, passing a check must always beat failing it.

        Not "a correct check never lowers trust" in absolute terms - the
        decay-toward-prior step (applied identically either way) can itself
        nudge a near-certain profile's score down by a hair even on a pass,
        which is intentional (see the module docstring: scores must stay
        adaptive, not calcify at the extremes). What must always hold is the
        *relative* comparison between the two possible outcomes.
        """
        starting = _FakeConsensusProfile(trust_alpha=alpha, trust_beta=beta)
        after_correct = _apply_check(_FakeConsensusProfile(trust_alpha=alpha, trust_beta=beta), correct=True)
        after_incorrect = _apply_check(starting, correct=False)
        self.assertGreater(after_correct.trust_score, after_incorrect.trust_score)

    def test_a_run_of_failures_drags_a_trusted_profile_down(self) -> None:
        """A previously-trusted profile that starts failing checks should visibly lose trust.

        Values verified numerically (not just asserted on faith): starting
        at alpha=20/beta=2 (trust_score ~0.909, a solidly-trusted profile),
        20 consecutive failures land at ~0.429 - a real, substantial drop
        that crosses below the neutral 0.5 midpoint within a few dozen
        checks, satisfying "a trusted player who starts answering wrong
        should lose trust" without requiring an unrealistically long losing
        streak.
        """
        profile = _FakeConsensusProfile(trust_alpha=20.0, trust_beta=2.0)
        starting_score = profile.trust_score
        self.assertGreater(starting_score, 0.9)
        for _ in range(20):
            profile = _apply_check(profile, correct=False)
        self.assertLess(profile.trust_score, 0.5)
        self.assertLess(profile.trust_score, starting_score)

    def test_a_run_of_successes_recovers_a_distrusted_profile(self) -> None:
        """Mirrors ``test_a_run_of_failures_drags_a_trusted_profile_down`` in the opposite direction."""
        profile = _FakeConsensusProfile(trust_alpha=2.0, trust_beta=20.0)
        starting_score = profile.trust_score
        self.assertLess(starting_score, 0.1)
        for _ in range(20):
            profile = _apply_check(profile, correct=True)
        self.assertGreater(profile.trust_score, 0.5)
        self.assertGreater(profile.trust_score, starting_score)


class CheckProbabilityTests(SimpleTestCase):
    @given(score=st.floats(min_value=-5, max_value=5))
    @settings(**_HYP)
    def test_always_within_floor_and_ceiling(self, score: float) -> None:
        probability = check_probability(score)
        self.assertGreaterEqual(probability, CHECK_PROBABILITY_MIN)
        self.assertLessEqual(probability, CHECK_PROBABILITY_MAX)

    @given(a=st.floats(min_value=0.0, max_value=1.0), b=st.floats(min_value=0.0, max_value=1.0))
    @settings(**_HYP)
    def test_monotonically_nonincreasing_in_trust_score(self, a: float, b: float) -> None:
        lower, higher = sorted((a, b))
        self.assertGreaterEqual(check_probability(lower), check_probability(higher) - 1e-9)

    def test_perfect_trust_gets_the_floor(self) -> None:
        self.assertAlmostEqual(check_probability(1.0), CHECK_PROBABILITY_MIN)

    def test_zero_trust_gets_the_ceiling(self) -> None:
        self.assertAlmostEqual(check_probability(0.0), CHECK_PROBABILITY_MAX)
