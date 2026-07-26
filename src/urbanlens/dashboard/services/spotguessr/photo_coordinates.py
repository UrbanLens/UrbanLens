"""Recording anonymized coordinate guesses toward a photo's own position.

See ``services.photo_coordinates`` for how these accumulate into an
estimate; this module is only the SpotGuessr-side hook that records every
Photos-mode guess and decides whether it's also worth feeding into that
estimate. See ``docs/designs/spotguessr.md``'s "Crowd-sourced photo
coordinates" for the full design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.spotguessr.model import PhotoCoordinateGuess
from urbanlens.dashboard.services.photo_coordinates import recompute_estimated_coordinates

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.spotguessr.model import GameRound


def record_guess(round_: GameRound, guess_point: Point, distance: float) -> None:
    """Anonymously record one guess toward ``round_``'s photo's own coordinates.

    Recorded for every Photos-mode round regardless of whether the photo
    already has its own coordinates - a photo that's already placed still
    gets its guesses saved (no current use, but plausibly useful later for,
    e.g., flagging/correcting a wrong placement); only the *estimate*
    recompute below stays conditional, since it would be moot for a photo
    that isn't relying on it. A no-op only for Named Place/Street View
    rounds, which have no ``round_.image`` at all.

    Deliberately takes no ``profile``: per spec, only the guessed
    coordinate, a correct/incorrect flag, and a timestamp are ever recorded,
    never who made it.

    Args:
        round_: The round the guess was submitted for.
        guess_point: Where the player clicked or picked from pin search.
        distance: The already-computed distance for this guess
            (``scoring.distance_for_guess``'s result) - reused rather than
            recomputed. For a boundary-target round, that's already exactly
            "distance from the location's effective boundary, 0 if inside" -
            this feature's own definition of "correct", deliberately
            independent of how the round is scored for gameplay. For a
            point-target round (photo already placed), 0 instead means
            "guessed the exact point" - a much rarer bar, but the same
            underlying value and consistent with how scoring treats the two
            cases everywhere else.
    """
    if round_.image_id is None:
        return

    is_correct = distance <= 0.0
    PhotoCoordinateGuess.objects.create(image_id=round_.image_id, guess_point=guess_point, is_correct=is_correct)
    if is_correct:
        from urbanlens.dashboard.services.facts.evidence import record_photo_coordinate_evidence

        record_photo_coordinate_evidence(round_.image_id, guess_point)
    if is_correct and not round_.target_is_point:
        recompute_estimated_coordinates(round_.image_id)
