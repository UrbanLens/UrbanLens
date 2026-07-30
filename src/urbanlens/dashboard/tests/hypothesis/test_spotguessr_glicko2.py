"""Tests for the Glicko-2 rating engine (services.spotguessr.glicko2).

Verified against Glickman's own worked example in "Example of the Glicko-2
system" (2012): a player rated 1500/RD 200/volatility 0.06 plays three games
in one rating period against opponents rated (1400, RD 30), (1550, RD 100),
(1700, RD 300) with results win/loss/loss, and should land at approximately
rating 1464.06, RD 151.52, volatility 0.05999.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.spotguessr.glicko2 import Opponent, Rating, rate

_SCALE = 173.7178


def _to_internal(display_rating: float, display_rd: float) -> tuple[float, float]:
    return (display_rating - 1500.0) / _SCALE, display_rd / _SCALE


def _to_display(mu: float, phi: float) -> tuple[float, float]:
    return 1500.0 + _SCALE * mu, _SCALE * phi


class GlickmanWorkedExampleTests(SimpleTestCase):
    """The paper's own numeric example, verbatim."""

    def setUp(self) -> None:
        mu, phi = _to_internal(1500.0, 200.0)
        self.rating = Rating(mu=mu, phi=phi, sigma=0.06)

        opp1_mu, opp1_phi = _to_internal(1400.0, 30.0)
        opp2_mu, opp2_phi = _to_internal(1550.0, 100.0)
        opp3_mu, opp3_phi = _to_internal(1700.0, 300.0)
        self.opponents = [
            Opponent(mu=opp1_mu, phi=opp1_phi, score=1.0),
            Opponent(mu=opp2_mu, phi=opp2_phi, score=0.0),
            Opponent(mu=opp3_mu, phi=opp3_phi, score=0.0),
        ]

    def test_matches_the_papers_published_result(self) -> None:
        updated = rate(self.rating, self.opponents)
        new_rating, new_rd = _to_display(updated.mu, updated.phi)
        self.assertAlmostEqual(new_rating, 1464.06, delta=0.01)
        self.assertAlmostEqual(new_rd, 151.52, delta=0.01)
        self.assertAlmostEqual(updated.sigma, 0.05999, delta=0.0001)


class RateBehaviorTests(SimpleTestCase):
    """Sanity properties that must hold regardless of the exact numbers."""

    def test_beating_a_higher_rated_opponent_raises_rating(self) -> None:
        rating = Rating(mu=0.0, phi=1.1513, sigma=0.06)
        stronger_opponent = Opponent(mu=1.0, phi=0.5, score=1.0)
        updated = rate(rating, [stronger_opponent])
        self.assertGreater(updated.mu, rating.mu)

    def test_losing_to_a_lower_rated_opponent_lowers_rating(self) -> None:
        rating = Rating(mu=0.0, phi=1.1513, sigma=0.06)
        weaker_opponent = Opponent(mu=-1.0, phi=0.5, score=0.0)
        updated = rate(rating, [weaker_opponent])
        self.assertLess(updated.mu, rating.mu)

    def test_playing_reduces_uncertainty(self) -> None:
        rating = Rating(mu=0.0, phi=1.1513, sigma=0.06)
        opponent = Opponent(mu=0.0, phi=1.1513, score=0.5)
        updated = rate(rating, [opponent])
        self.assertLess(updated.phi, rating.phi)

    def test_no_games_only_grows_uncertainty(self) -> None:
        rating = Rating(mu=0.25, phi=0.8, sigma=0.06)
        updated = rate(rating, [])
        self.assertEqual(updated.mu, rating.mu)
        self.assertEqual(updated.sigma, rating.sigma)
        self.assertGreater(updated.phi, rating.phi)

    def test_a_draw_against_an_equal_opponent_leaves_rating_unchanged(self) -> None:
        rating = Rating(mu=0.3, phi=0.9, sigma=0.06)
        mirror_opponent = Opponent(mu=0.3, phi=0.9, score=0.5)
        updated = rate(rating, [mirror_opponent])
        self.assertAlmostEqual(updated.mu, rating.mu, places=9)
