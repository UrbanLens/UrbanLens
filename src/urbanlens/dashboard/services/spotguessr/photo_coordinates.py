"""Recording anonymized coordinate guesses toward an unplaced photo's own position.

See ``services.photo_coordinates`` for how these accumulate into an
estimate; this module is only the SpotGuessr-side hook deciding whether a
given guess is worth recording at all. See
``docs/designs/spotguessr.md``'s "Crowd-sourced photo coordinates" for the
full design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.spotguessr.model import PhotoCoordinateGuess
from urbanlens.dashboard.services.photo_coordinates import recompute_estimated_coordinates

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.spotguessr.model import GameRound


def record_guess(round_: GameRound, guess_point: Point, distance: float) -> None:
    """Anonymously record one guess toward ``round_``'s photo's own coordinates, if it's still unplaced.

    A no-op for Named Place/Street View rounds (no ``round_.image``) and for
    any Photos-mode round whose photo already had its own coordinates when
    this round was generated (``round_.target_is_point``) - once a photo has
    real coordinates, crowd-sourcing an estimate for it serves no purpose.

    Deliberately takes no ``profile``: per spec, only the guessed
    coordinate, a correct/incorrect flag, and a timestamp are ever recorded,
    never who made it.

    Args:
        round_: The round the guess was submitted for.
        guess_point: Where the player clicked or picked from pin search.
        distance: The already-computed distance for this guess
            (``scoring.distance_for_guess``'s result) - reused rather than
            recomputed. For a round with no ``target_is_point`` (guaranteed
            by the check above), that's already exactly "distance from the
            location's effective boundary, 0 if inside" - precisely this
            feature's own definition of "correct", which is deliberately
            independent of how the round itself is scored for gameplay.
    """
    if round_.target_is_point or round_.image_id is None:
        return

    is_correct = distance <= 0.0
    PhotoCoordinateGuess.objects.create(image_id=round_.image_id, guess_point=guess_point, is_correct=is_correct)
    if is_correct:
        recompute_estimated_coordinates(round_.image_id)
