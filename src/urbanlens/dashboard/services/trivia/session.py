"""Session orchestration: solo lifecycle (Phase 1), round generation, answer scoring.

The one place ``controllers.trivia`` calls into - mirrors
``services.spotguessr.session``'s shape, but Phase 1 only implements solo
play; multiplayer entry points (``start_multiplayer_session``,
``invite_to_session``, ``join_session``, ``begin_session``) are a follow-up
phase, same as SpotGuessr's own UL-392 multiplayer work was.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from django.contrib.gis.geos import GEOSGeometry
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from urbanlens.dashboard.models.trivia.model import (
    TriviaAnswer,
    TriviaAnswerMatchKind,
    TriviaQuestion,
    TriviaRound,
    TriviaSession,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
    TriviaSessionStatus,
)
from urbanlens.dashboard.services.trivia import eligibility, selection, voting
from urbanlens.dashboard.services.trivia.ratings import apply_round_ratings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

DEFAULT_ROUNDS_PER_SESSION = 5
MIN_ROUNDS_PER_SESSION = 3
MAX_ROUNDS_PER_SESSION = 20

#: Flat points for a correct answer - unlike SpotGuessr's distance-decay
#: curve, a Trivia answer is a binary right/wrong, so there's no continuous
#: closeness to curve (Phase 3's AI-judged "close enough" match is still
#: either right or wrong, just judged more leniently - it doesn't introduce
#: partial credit).
POINTS_FOR_CORRECT_ANSWER = 1000


class TriviaError(Exception):
    """Raised for invalid session/round/answer operations."""


@dataclass(frozen=True)
class TriviaConfig:
    """A validated, session-ready snapshot of Trivia settings. Mirrors ``spotguessr.session.GameConfig``."""

    difficulty: float = 0.5
    geo_bounds_geojson: dict | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for ``TriviaSession.config``."""
        return dataclasses.asdict(self)

    @property
    def geo_bounds(self) -> GEOSGeometry | None:
        """The configured geographic restriction as a GEOS geometry, or None."""
        if not self.geo_bounds_geojson:
            return None
        return GEOSGeometry(json.dumps(self.geo_bounds_geojson))


def _config_from_session(session: TriviaSession) -> TriviaConfig:
    """Reconstruct a TriviaConfig from a session's stored config snapshot, ignoring unknown keys."""
    known_fields = {f.name for f in dataclasses.fields(TriviaConfig)}
    return TriviaConfig(**{key: value for key, value in (session.config or {}).items() if key in known_fields})


def _clamp_rounds(total_rounds: int) -> int:
    return max(MIN_ROUNDS_PER_SESSION, min(MAX_ROUNDS_PER_SESSION, total_rounds))


def start_solo_session(profile: Profile, config: TriviaConfig, *, total_rounds: int = DEFAULT_ROUNDS_PER_SESSION) -> TriviaSession:
    """Create a new single-participant, immediately-ACTIVE Trivia session for ``profile``."""
    session = TriviaSession.objects.create(
        host_profile=profile,
        status=TriviaSessionStatus.ACTIVE,
        config=config.to_dict(),
        total_rounds=_clamp_rounds(total_rounds),
    )
    TriviaSessionParticipant.objects.create(session=session, profile=profile, status=TriviaSessionParticipantStatus.JOINED)
    return session


def get_or_create_round(session: TriviaSession) -> TriviaRound | None:
    """Return the session's current round, creating the next one once the prior round is fully answered.

    Only JOINED participants count - an invitee who never accepted is not a
    player (mirrors ``GameSessionParticipant``'s rule; irrelevant for Phase
    1's solo-only sessions, kept for Phase 2 multiplayer to reuse unchanged).

    Returns:
        The round to play/show next, or None when the session is complete
        (every configured round was played) or has run out of eligible,
        in-rotation questions - either way, the caller should treat None as
        "call ``complete_session``."
    """
    config = _config_from_session(session)
    joined_participants = list(session.participants.joined().select_related("profile"))
    participant_count = len(joined_participants)
    if participant_count == 0:
        return None

    existing_rounds = list(TriviaRound.objects.for_session(session).select_related("question"))
    if existing_rounds:
        last_round = existing_rounds[-1]
        if TriviaAnswer.objects.for_round(last_round).count() < participant_count:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    participants = [participant.profile for participant in joined_participants]
    excluded_question_ids = [round_.question_id for round_ in existing_rounds]

    candidates = eligibility.eligible_questions(participants, geo_bounds=config.geo_bounds, exclude_question_ids=excluded_question_ids)
    question = selection.pick_next_question(candidates, difficulty=config.difficulty)
    if question is None:
        return None  # nothing eligible left at all

    return TriviaRound.objects.create(session=session, sequence_index=len(existing_rounds), question=question)


def submit_answer(round_: TriviaRound, profile: Profile, raw_answer: str) -> TriviaAnswer:
    """Score and record ``profile``'s answer for ``round_``.

    Triggers the Glicko-2 rating update (``apply_round_ratings``) and the
    question's ``NO_REACTION`` vote backfill once every joined participant
    has answered, then eagerly advances to the next round (or completes the
    session) - mirrors ``services.spotguessr.session.submit_guess``. Phase 1
    only ever exact-matches (``matched_via=EXACT``); a follow-up phase adds
    an AI fallback here for a normalized mismatch.

    Raises:
        TriviaError: if ``profile`` already answered this round.
    """
    question = round_.question
    is_correct = TriviaQuestion.normalize_answer(raw_answer) == question.answer_normalized
    points = POINTS_FOR_CORRECT_ANSWER if is_correct else 0

    session = round_.session

    # Two participants can submit their round-completing answer at nearly
    # the same instant - select_for_update() serializes the read-count-decide
    # critical section per round, same race guard as SpotGuessr's submit_guess.
    round_completed_now = False
    with transaction.atomic():
        locked_round = TriviaRound.objects.select_for_update().get(pk=round_.pk)
        try:
            answer = TriviaAnswer.objects.create(
                round=locked_round,
                profile=profile,
                raw_answer=raw_answer,
                is_correct=is_correct,
                matched_via=TriviaAnswerMatchKind.EXACT,
                points=points,
            )
        except IntegrityError:
            raise TriviaError("This profile has already answered this round.") from None

        TriviaSessionParticipant.objects.filter(session=session, profile=profile).update(total_points=F("total_points") + points)

        joined_count = session.participants.joined().count()
        if locked_round.revealed_at is None and TriviaAnswer.objects.for_round(locked_round).count() >= joined_count:
            locked_round.revealed_at = timezone.now()
            locked_round.save(update_fields=["revealed_at", "updated"])
            round_completed_now = True

    if round_completed_now:
        round_.refresh_from_db()
        completed_answers = list(TriviaAnswer.objects.for_round(round_).select_related("profile"))
        apply_round_ratings(round_, completed_answers)
        voting.backfill_no_reaction(round_.question, [answer.profile for answer in completed_answers])

        if get_or_create_round(session) is None:
            complete_session(session)

    return answer


def rounds_played(session: TriviaSession) -> int:
    """How many rounds this session has ever created."""
    return TriviaRound.objects.for_session(session).count()


def complete_session(session: TriviaSession) -> TriviaSession:
    """Mark a session finished (all rounds played, or no eligible questions remained)."""
    if session.status == TriviaSessionStatus.ACTIVE:
        session.status = TriviaSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
    return session


def session_summary(session: TriviaSession) -> dict:
    """A JSON-ready summary: rounds played and per-(joined)-participant totals."""
    participants = session.participants.joined().select_related("profile__user").order_by("-total_points")
    return {
        "session_id": session.pk,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "rounds_played": rounds_played(session),
        "participants": [
            {
                "profile_id": participant.profile_id,
                "username": participant.profile.user.username,
                "avatar_url": participant.profile.avatar.url if participant.profile.avatar else None,
                "total_points": participant.total_points,
            }
            for participant in participants
        ],
    }
