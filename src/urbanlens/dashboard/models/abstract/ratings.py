"""Glicko-2 rating storage shared by every game that rates players against content."""

from __future__ import annotations

from typing import TYPE_CHECKING

#: Glicko-2's internal scale <-> the traditional (Elo-familiar) display scale,
#: per Glickman's "Example of the Glicko-2 system" (2012).
GLICKO2_SCALE = 173.7178
DEFAULT_RATING = 1500.0
DEFAULT_RATING_DEVIATION = 350.0
DEFAULT_VOLATILITY = 0.06

#: Defaults for the stored internal-scale fields.
DEFAULT_MU = 0.0
DEFAULT_PHI = DEFAULT_RATING_DEVIATION / GLICKO2_SCALE


class Glicko2RatingFields:
    """Display-scale conversion for a model storing ``mu``/``phi``/``sigma``.

    Those are kept on the paper's internal scale (mu centered on 0, phi around 1-2), which ``services.games.glicko2``
    operates on directly; everything user-facing reads ``rating``/``rating_deviation``.
    """

    if TYPE_CHECKING:
        mu: float
        phi: float

    @property
    def rating(self) -> float:
        """Display-scale rating (Elo/Glicko-familiar, centered on 1500)."""
        return DEFAULT_RATING + GLICKO2_SCALE * self.mu

    @property
    def rating_deviation(self) -> float:
        """Display-scale rating deviation (uncertainty; lower = more confident)."""
        return GLICKO2_SCALE * self.phi
