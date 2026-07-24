"""Session orchestration: starting a session, generating rounds, scoring guesses.

The one place ``controllers.spotguessr`` calls into - the only layer that
knows how eligibility, selection, photo-picking, scoring, and ratings
compose together. See ``docs/designs/spotguessr.md`` for the full rules.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from django.contrib.gis.geos import GEOSGeometry
from django.db.models import F
from django.utils import timezone

from urbanlens.dashboard.models.spotguessr.model import (
    GameRound,
    GameSession,
    GameSessionParticipant,
    GameSessionStatus,
    Guess,
    SpotGuessrMode,
)
from urbanlens.dashboard.services.spotguessr import eligibility, photos, scoring, selection
from urbanlens.dashboard.services.spotguessr.ratings import apply_round_ratings

if TYPE_CHECKING:
    from datetime import date

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.profile.model import Profile

DEFAULT_ROUNDS_PER_SESSION = 5
MIN_ROUNDS_PER_SESSION = 3
MAX_ROUNDS_PER_SESSION = 20

#: How many locations to try before giving up on generating a round - guards
#: against looping forever when a whole eligible pool turns out to have no
#: usable photo (Photos mode) without erroring the whole session.
_MAX_LOCATION_ATTEMPTS = 25


class SpotGuessrError(Exception):
    """Raised for invalid session/round/guess operations."""


@dataclass(frozen=True)
class GameConfig:
    """A validated, session-ready snapshot of SpotGuessr settings.

    Mirrors what's stored on ``GameSession.config`` - see
    ``docs/designs/spotguessr.md``'s config table for defaults.
    """

    difficulty: float = 0.5
    external_media_only: bool = False
    require_visited_all: bool = False
    date_guessing_enabled: bool = False
    geo_bounds_geojson: dict | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for ``GameSession.config``."""
        return dataclasses.asdict(self)

    @property
    def geo_bounds(self) -> GEOSGeometry | None:
        """The configured geographic restriction as a GEOS geometry, or None."""
        if not self.geo_bounds_geojson:
            return None
        return GEOSGeometry(json.dumps(self.geo_bounds_geojson))


def _config_from_session(session: GameSession) -> GameConfig:
    """Reconstruct a GameConfig from a session's stored config snapshot, ignoring unknown keys."""
    known_fields = {f.name for f in dataclasses.fields(GameConfig)}
    return GameConfig(**{key: value for key, value in (session.config or {}).items() if key in known_fields})


def start_solo_session(profile: Profile, mode: str, config: GameConfig, *, total_rounds: int = DEFAULT_ROUNDS_PER_SESSION) -> GameSession:
    """Create a new single-participant SpotGuessr session for ``profile``."""
    if mode != SpotGuessrMode.PHOTOS:
        raise SpotGuessrError(f"Mode {mode!r} is not yet implemented (see docs/designs/spotguessr.md's phase mapping).")

    clamped_rounds = max(MIN_ROUNDS_PER_SESSION, min(MAX_ROUNDS_PER_SESSION, total_rounds))
    session = GameSession.objects.create(
        host_profile=profile,
        mode=mode,
        config=config.to_dict(),
        total_rounds=clamped_rounds,
    )
    GameSessionParticipant.objects.create(session=session, profile=profile)
    return session


def get_or_create_round(session: GameSession) -> GameRound | None:
    """Return the session's current round, creating the next one once the prior round is fully guessed.

    Returns:
        The round to play/show next, or None when the session is complete
        (every configured round was played) or has run out of eligible,
        playable locations - either way, the caller should treat None as
        "call ``complete_session``."
    """
    config = _config_from_session(session)
    participant_count = session.participants.count()

    existing_rounds = list(GameRound.objects.for_session(session).select_related("location", "image"))
    if existing_rounds:
        last_round = existing_rounds[-1]
        if Guess.objects.for_round(last_round).count() < participant_count:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    participants = [participant.profile for participant in session.participants.select_related("profile").all()]
    excluded_ids = [round_.location_id for round_ in existing_rounds]
    previous_location = existing_rounds[-1].location if existing_rounds else None

    for _attempt in range(_MAX_LOCATION_ATTEMPTS):
        candidates = eligibility.eligible_locations(
            participants,
            require_visited_by_all=config.require_visited_all,
            geo_bounds=config.geo_bounds,
            exclude_location_ids=excluded_ids,
        )
        location = selection.pick_next_location(candidates, mode=session.mode, difficulty=config.difficulty, previous_location=previous_location)
        if location is None:
            return None  # nothing eligible left at all

        image = None
        if session.mode == SpotGuessrMode.PHOTOS:
            image = photos.candidate_image_for_location(location, external_media_only=config.external_media_only)
            if image is None:
                excluded_ids.append(location.pk)
                continue  # this location has no usable photo yet - try another

        target = scoring.resolve_target(location, image)
        return GameRound.objects.create(
            session=session,
            sequence_index=len(existing_rounds),
            location=location,
            image=image,
            target_is_point=target.is_point,
            target_point=target.geometry if target.is_point else None,
        )

    return None


def submit_guess(round_: GameRound, profile: Profile, guess_point: Point, guessed_date: date | None = None) -> Guess:
    """Score and record ``profile``'s guess for ``round_``.

    Triggers the Glicko-2 rating update (``apply_round_ratings``) once
    every participant in the round has guessed.

    Raises:
        SpotGuessrError: if ``profile`` already guessed this round.
    """
    if Guess.objects.filter(round=round_, profile=profile).exists():
        raise SpotGuessrError("This profile has already guessed this round.")

    distance = scoring.distance_for_guess(
        round_.location,
        guess_point,
        target_is_point=round_.target_is_point,
        target_point=round_.target_point,
    )
    points = scoring.points_for_distance(distance)

    session = round_.session
    config = _config_from_session(session)
    date_points = 0
    if config.date_guessing_enabled and guessed_date is not None and round_.image is not None and round_.image.taken_at is not None:
        date_points = scoring.points_for_date_guess(guessed_date, round_.image.taken_at.date())

    guess = Guess.objects.create(
        round=round_,
        profile=profile,
        guess_point=guess_point,
        distance_meters=distance,
        points=points,
        guessed_date=guessed_date,
        date_points=date_points,
    )

    GameSessionParticipant.objects.filter(session=session, profile=profile).update(total_points=F("total_points") + points + date_points)

    if Guess.objects.for_round(round_).count() >= session.participants.count():
        round_.revealed_at = timezone.now()
        round_.save(update_fields=["revealed_at", "updated"])
        apply_round_ratings(round_, list(Guess.objects.for_round(round_).select_related("profile")))

    return guess


def complete_session(session: GameSession) -> GameSession:
    """Mark a session finished (all rounds played, or no eligible locations remained)."""
    if session.status == GameSessionStatus.ACTIVE:
        session.status = GameSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
    return session


def session_summary(session: GameSession) -> dict:
    """A JSON-ready summary: rounds played and per-participant totals."""
    rounds_played = GameRound.objects.for_session(session).count()
    participants = session.participants.select_related("profile__user").order_by("-total_points")
    return {
        "session_id": session.pk,
        "mode": session.mode,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "rounds_played": rounds_played,
        "participants": [
            {"profile_id": participant.profile_id, "username": participant.profile.user.username, "total_points": participant.total_points}
            for participant in participants
        ],
    }
