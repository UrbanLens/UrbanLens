"""Glicko-2 rating math (Glickman, "Example of the Glicko-2 system", 2012).

Pure math, deliberately with no Django/ORM dependency - ``services.spotguessr.ratings``
is the layer that reads/writes ``PlayerModeRating``/``LocationModeRating`` rows around this.

SpotGuessr repurposes plain, unmodified Glicko-2 as a symmetric player-skill /
location-difficulty pairing (see ``docs/designs/spotguessr.md``): a round is one
rating period for both the player (opponent = the location, score = normalized
points) and the location (opponents = every participant, score = ``1 - their
normalized points``). Only the *meaning* of "opponent" and "score" is chosen to
fit the game; the algorithm below is the paper's, unmodified.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Recommended system constant (0.3-1.2 per the paper); controls how much a
#: single surprising result can move volatility. Must match
#: ``docs/designs/spotguessr.md``'s config table.
DEFAULT_TAU = 0.5

#: The paper's Illinois-algorithm root find stops once the bracket is this narrow.
CONVERGENCE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Rating:
    """A rating on Glicko-2's own internal scale (mu centered on 0, phi ~1-2)."""

    mu: float
    phi: float
    sigma: float


@dataclass(frozen=True)
class Opponent:
    """One game result to rate against: an opponent's rating plus the outcome score.

    ``score`` is in [0, 1] - 1.0 is a full win, 0.0 a full loss, and (unlike
    plain Elo) any fraction in between is a legitimate, meaningful result:
    SpotGuessr uses it directly as "how close was the guess," not just as a
    draw indicator.
    """

    mu: float
    phi: float
    score: float


def _g(phi: float) -> float:
    """The paper's ``g(phi)`` - down-weights an opponent's influence when their RD is high."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi**2 / math.pi**2)


def _expected_score(mu: float, opponent_mu: float, opponent_phi: float) -> float:
    """The paper's ``E`` - probability-of-winning estimate given both ratings."""
    return 1.0 / (1.0 + math.exp(-_g(opponent_phi) * (mu - opponent_mu)))


def rate(rating: Rating, opponents: Sequence[Opponent], *, tau: float = DEFAULT_TAU) -> Rating:
    """Apply one Glicko-2 rating period update.

    Args:
        rating: The rating entering the period.
        opponents: Every game result within the period. Empty means "sat out
            this rating period" - per the paper, only ``phi`` grows (rating
            uncertainty increases with inactivity); ``mu``/``sigma`` are
            unchanged. SpotGuessr never actually calls this with an empty
            list (a round only rates parties who played it), but the branch
            exists for correctness/testability against the paper.
        tau: System volatility constant.

    Returns:
        The updated rating.
    """
    if not opponents:
        return Rating(mu=rating.mu, phi=math.sqrt(rating.phi**2 + rating.sigma**2), sigma=rating.sigma)

    variance_terms = []
    delta_terms = []
    for opponent in opponents:
        g_j = _g(opponent.phi)
        e_j = _expected_score(rating.mu, opponent.mu, opponent.phi)
        variance_terms.append(g_j**2 * e_j * (1.0 - e_j))
        delta_terms.append(g_j * (opponent.score - e_j))

    v = 1.0 / sum(variance_terms)
    delta = v * sum(delta_terms)

    sigma_prime = _new_volatility(rating.phi, rating.sigma, v, delta, tau)

    phi_star = math.sqrt(rating.phi**2 + sigma_prime**2)
    phi_prime = 1.0 / math.sqrt(1.0 / phi_star**2 + 1.0 / v)
    mu_prime = rating.mu + phi_prime**2 * sum(delta_terms)

    return Rating(mu=mu_prime, phi=phi_prime, sigma=sigma_prime)


def _new_volatility(phi: float, sigma: float, v: float, delta: float, tau: float) -> float:
    """Solve for the period's new volatility via the paper's Illinois-algorithm root find.

    Finds the root of ``f`` (the paper's eq. (5)) bracketed between the
    current log-variance and a bound chosen so the root is guaranteed to lie
    within it, then narrows the bracket until it's within
    ``CONVERGENCE_TOLERANCE``.
    """
    a = math.log(sigma**2)

    def f(x: float) -> float:
        ex = math.exp(x)
        numerator = ex * (delta**2 - phi**2 - v - ex)
        denominator = 2.0 * (phi**2 + v + ex) ** 2
        return numerator / denominator - (x - a) / tau**2

    lower = a
    if delta**2 > phi**2 + v:
        upper = math.log(delta**2 - phi**2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        upper = a - k * tau

    f_lower = f(lower)
    f_upper = f(upper)

    while abs(upper - lower) > CONVERGENCE_TOLERANCE:
        midpoint = lower + (lower - upper) * f_lower / (f_upper - f_lower)
        f_mid = f(midpoint)
        if f_mid * f_upper < 0:
            lower, f_lower = upper, f_upper
        else:
            f_lower = f_lower / 2.0
        upper, f_upper = midpoint, f_mid

    return math.exp(lower / 2.0)
