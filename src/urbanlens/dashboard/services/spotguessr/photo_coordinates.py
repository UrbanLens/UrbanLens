"""Recording anonymized coordinate guesses toward a photo's own position.
See ``services.photos.photo_coordinates`` for how these accumulate into an estimate; this module is only the SpotGuessr-side hook that records every Photos-mode guess and decides whether it's also worth feeding into that estimate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.spotguessr.model import PhotoCoordinateGuess
from urbanlens.dashboard.services.photos.photo_coordinates import recompute_estimated_coordinates

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.spotguessr.model import GameRound


def record_guess(round_: GameRound, guess_point: Point, distance: float) -> None:
    """Anonymously record one guess toward ``round_``'s photo's own coordinates.

    Args:
        round_: The round the guess was submitted for.
        guess_point: Where the player clicked or picked from pin search.
        distance: The already-computed distance for this guess (``scoring.distance_for_guess``'s result) - reused rather than recomputed."""
    if round_.image_id is None:
        return

    is_correct = distance <= 0.0
    PhotoCoordinateGuess.objects.create(image_id=round_.image_id, guess_point=guess_point, is_correct=is_correct)
    if is_correct:
        from urbanlens.dashboard.services.facts.evidence import record_photo_coordinate_evidence

        record_photo_coordinate_evidence(round_.image_id, guess_point)
    if is_correct and not round_.target_is_point:
        recompute_estimated_coordinates(round_.image_id)
