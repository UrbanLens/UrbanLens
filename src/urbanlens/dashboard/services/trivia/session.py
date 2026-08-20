"""Session orchestration: solo + multiplayer lifecycle, round generation, answer scoring.

The one place ``controllers.trivia``/``consumers.TriviaSessionConsumer`` call
into - mirrors ``services.spotguessr.session``'s shape exactly, including the
multiplayer lobby lifecycle (``start_multiplayer_session``,
``invite_to_session``, ``join_session``, ``begin_session``).
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
from urbanlens.dashboard.services.social.connections import are_connections
from urbanlens.dashboard.services.trivia import eligibility, realtime, selection, serializers, voting
from urbanlens.dashboard.services.trivia.answer_check import is_answer_equivalent
from urbanlens.dashboard.services.trivia.ratings import apply_round_ratings

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.profile.model import Profile

DEFAULT_ROUNDS_PER_SESSION = 5
MIN_ROUNDS_PER_SESSION = 3
MAX_ROUNDS_PER_SESSION = 20

#: How long a round may sit unrevealed before the stall-sweep Celery task
#: (``tasks.sweep_stalled_trivia_sessions``) force-reveals it. Mirrors
#: ``services.spotguessr.session.STALL_ROUND_TIMEOUT_MINUTES``.
STALL_ROUND_TIMEOUT_MINUTES = 10

#: Flat points for a correct answer - unlike SpotGuessr's distance-decay
#: curve, a Trivia answer is a binary right/wrong, so there's no continuous
#: closeness to curve (Phase 3's AI-judged "close enough" match is still
#: either right or wrong, just judged more leniently - it doesn't introduce
#: partial credit).
POINTS_FOR_CORRECT_ANSWER = 1000


class TriviaError(Exception):
    """Raised for invalid session/round/answer operations.

    ``safe_message`` is always safe to surface to the caller verbatim - every
    raise site in this module passes a developer-authored string.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


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
        """The configured geographic restriction as a GEOS geometry, or None.

        Split at the antimeridian here rather than at each query: the callers all
        run planar ``__within`` lookups (``ST_Within`` has no geography
        implementation), and an area a player drew across the date line arrives
        with unwrapped coordinates that match nothing on its far side. Splitting
        at the source means every consumer - eligibility counts, round selection,
        the external API - inherits the fix.

        Returns:
            The restriction geometry, or None when unrestricted.
        """
        if not self.geo_bounds_geojson:
            return None
        from urbanlens.dashboard.services.geo.longitude import split_at_antimeridian

        return split_at_antimeridian(GEOSGeometry(json.dumps(self.geo_bounds_geojson)))


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


def start_multiplayer_session(
    host: Profile,
    config: TriviaConfig,
    invite_profiles: Iterable[Profile],
    *,
    total_rounds: int = DEFAULT_ROUNDS_PER_SESSION,
) -> TriviaSession:
    """Create a LOBBY session hosted by ``host`` and invite the given (friend) profiles.

    The host's own participant row is created JOINED immediately - no
    invite step for yourself. Each invitee gets an INVITED row plus a
    notification (see ``_notify_invite``). Mirrors
    ``services.spotguessr.session.start_multiplayer_session``.
    """
    session = TriviaSession.objects.create(
        host_profile=host,
        status=TriviaSessionStatus.LOBBY,
        config=config.to_dict(),
        total_rounds=_clamp_rounds(total_rounds),
    )
    TriviaSessionParticipant.objects.create(session=session, profile=host, status=TriviaSessionParticipantStatus.JOINED)
    for invitee in invite_profiles:
        invite_to_session(session, host, invitee)
    return session


def invite_to_session(session: TriviaSession, host: Profile, invitee: Profile) -> TriviaSessionParticipant:
    """Invite one friend to a lobby session, notifying them.

    Host-only, friends-only (matching the SpotGuessr precedent) - inviting a
    non-friend is rejected server-side, not just hidden in a picker UI.

    Raises:
        TriviaError: if the caller isn't the host, the session has already
            started, or ``invitee`` isn't a friend of the host.
    """
    if session.host_profile_id != host.pk:
        raise TriviaError("Only the host can invite players.")
    if session.status != TriviaSessionStatus.LOBBY:
        raise TriviaError("Can't invite once the game has started.")
    if not are_connections(host, invitee):
        raise TriviaError("You can only invite friends.")

    participant, created = TriviaSessionParticipant.objects.get_or_create(
        session=session,
        profile=invitee,
        defaults={"status": TriviaSessionParticipantStatus.INVITED},
    )
    if created:
        _notify_invite(host, invitee, session)
    elif participant.status == TriviaSessionParticipantStatus.LEFT:
        # A departed participant's row already exists (LEFT is terminal, see
        # the model docstring) - get_or_create would otherwise silently
        # return it unchanged instead of actually re-inviting them.
        participant.status = TriviaSessionParticipantStatus.INVITED
        participant.save(update_fields=["status", "updated"])
        _notify_invite(host, invitee, session)
    return participant


def _notify_invite(host: Profile, invitee: Profile, session: TriviaSession) -> None:
    """Create the in-app (+ live toast, via the existing NotificationLog signal) invite notification."""
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    host_name = resolve_visible_identity(invitee, host)["display_name"]
    NotificationLog.objects.notify(
        profile=invitee,
        source_profile=host,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.TRIVIA_INVITE,
        title="Trivia invitation",
        message=f"{host_name} invited you to play Trivia.",
        # "trivia.lobby" is a JSON API endpoint, not a page - link to the
        # game page itself with a query param it knows to pick up on load.
        url=f"{reverse('trivia')}?session={session.pk}",
    )


def join_session(session: TriviaSession, profile: Profile) -> TriviaSessionParticipant:
    """Accept an invitation - flips INVITED to JOINED and broadcasts to the lobby.

    Idempotent for a profile that's already JOINED (harmless re-POST, or a
    reconnecting participant). Only actually-new joins are rejected once the
    roster is locked.

    Raises:
        TriviaError: if ``profile`` was never invited to this session, or
            the roster is already locked (the session isn't in LOBBY) and
            they hadn't joined before that happened.
    """
    try:
        participant = TriviaSessionParticipant.objects.get(session=session, profile=profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise TriviaError("You were not invited to this session.") from None

    if participant.status == TriviaSessionParticipantStatus.JOINED:
        return participant

    if session.status != TriviaSessionStatus.LOBBY:
        raise TriviaError("This game has already started - you can no longer join.")

    participant.status = TriviaSessionParticipantStatus.JOINED
    participant.save(update_fields=["status", "updated"])
    realtime.broadcast(session.pk, "participant.joined", {"participant": serializers.serialize_participant(participant)})
    return participant


def begin_session(session: TriviaSession, host: Profile) -> TriviaRound | None:
    """Host starts the game: locks the roster, transitions LOBBY to ACTIVE, creates round 1.

    Unlike solo start, the joined roster's combined eligibility can't be
    checked before this point (invitees may not have joined yet). If
    locking the roster reveals there's nothing eligible for this group, the
    session is left ACTIVE (not marked COMPLETED) since it never actually
    played anything - the caller should report ``{"finished": false,
    "no_eligible_questions": true}``, mirroring ``SpotGuessrBeginView``.

    Raises:
        TriviaError: if the caller isn't the host or the session isn't
            still in its lobby.
    """
    if session.host_profile_id != host.pk:
        raise TriviaError("Only the host can start the game.")
    if session.status != TriviaSessionStatus.LOBBY:
        raise TriviaError("This session has already started.")

    session.status = TriviaSessionStatus.ACTIVE
    session.save(update_fields=["status", "updated"])
    round_ = get_or_create_round(session)
    if round_ is not None:
        realtime.broadcast(session.pk, "session.started", {"round": serializers.serialize_round(round_)})
    return round_


def get_or_create_round(session: TriviaSession) -> TriviaRound | None:
    """Return the session's current round, creating the next one once the prior round is fully answered.

    Only JOINED participants count - an invitee who never accepted is not a
    player.

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
        # A revealed round is finished no matter how many people answered it.
        # Testing only the answer count treated a *force*-revealed round (the
        # stall sweep's whole purpose - see force_reveal_round) as still in
        # progress, so it was handed back here forever: the session could
        # neither advance to the next round nor complete, because
        # _advance_or_complete only completes when this returns None.
        if last_round.revealed_at is None and TriviaAnswer.objects.for_round(last_round).count() < participant_count:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    participants = [participant.profile for participant in joined_participants]
    excluded_question_ids = [round_.question_id for round_ in existing_rounds]

    candidates = list(eligibility.eligible_questions(participants, geo_bounds=config.geo_bounds, exclude_question_ids=excluded_question_ids))

    # A solo player may very rarely see their own not-yet-approved question -
    # never in multiplayer, and never any other player's. See
    # eligibility.solo_own_pending_questions's docstring for the full spec
    # rationale (no feedback loop for the submitter to probe the filter).
    weight_overrides: dict[int, float] = {}
    if participant_count == 1:
        own_pending = list(eligibility.solo_own_pending_questions(participants[0], geo_bounds=config.geo_bounds, exclude_question_ids=excluded_question_ids))
        candidates.extend(own_pending)
        weight_overrides = dict.fromkeys((q.pk for q in own_pending), eligibility.OWN_UNAPPROVED_WEIGHT)

    question = selection.pick_next_question(candidates, difficulty=config.difficulty, weight_overrides=weight_overrides)
    if question is None:
        return None  # nothing eligible left at all

    return TriviaRound.objects.create(session=session, sequence_index=len(existing_rounds), question=question)


def submit_answer(round_: TriviaRound, profile: Profile, raw_answer: str) -> TriviaAnswer:
    """Score and record ``profile``'s answer for ``round_``.

    Triggers the Glicko-2 rating update (``apply_round_ratings``) and the
    question's ``NO_REACTION`` vote backfill once every joined participant
    has answered, then eagerly advances to the next round (or completes the
    session) - mirrors ``services.spotguessr.session.submit_guess``, including
    the real-time broadcast sequence (``answer.submitted`` immediately, then
    ``round.revealed`` + either ``round.started`` or ``session.completed``
    once the round completes; a no-op without a channel layer listener, so
    solo sessions work exactly the same as before). On a normalized-string
    mismatch, falls back to ``services.trivia.answer_check`` (gated on
    ``SiteFeature.AI`` - a profile without it simply gets exact-match-only,
    never blocked from playing).

    Raises:
        TriviaError: if ``profile`` isn't a JOINED participant of this
            round's session (e.g. still INVITED, never joined), or if
            ``profile`` already answered this round.
    """
    try:
        participant = TriviaSessionParticipant.objects.get(session=round_.session, profile=profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise TriviaError("You must join this session before submitting an answer.") from None
    if participant.status != TriviaSessionParticipantStatus.JOINED:
        raise TriviaError("You must join this session before submitting an answer.")

    question = round_.question
    is_correct = TriviaQuestion.normalize_answer(raw_answer) == question.answer_normalized
    matched_via = TriviaAnswerMatchKind.EXACT
    if not is_correct and is_answer_equivalent(raw_answer, question.answer, profile=profile):
        is_correct = True
        matched_via = TriviaAnswerMatchKind.AI
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
                matched_via=matched_via,
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

    realtime.broadcast(session.pk, "answer.submitted", {"profile_id": profile.pk})

    if round_completed_now:
        round_.refresh_from_db()
        completed_answers = list(TriviaAnswer.objects.for_round(round_).select_related("profile"))
        _finish_round(round_, completed_answers)
        _advance_or_complete(session)

    return answer


def _finish_round(round_: TriviaRound, completed_answers: list[TriviaAnswer]) -> None:
    """Rate, backfill vote signal, and broadcast the reveal for a just-completed round.

    Shared by ``submit_answer`` (the normal "everyone answered" path),
    ``force_reveal_round`` (the stall-sweep path), ``end_session_now``
    (the host-ended path), and the participant-removal path
    (``leave_session``/``kick_participant``, via ``_remove_participant``) -
    ``completed_answers`` may be a strict subset of the joined roster, or
    empty, in every path but the first. A participant with no answer this
    round simply isn't rated for it (see ``apply_round_ratings``, which only
    touches profiles present in ``completed_answers``); the reveal still
    broadcasts even with zero answers, so "nobody answered in time" reads as
    a normal (if empty) round result rather than silently stalling. Mirrors
    ``services.spotguessr.session._finish_round``.
    """
    if completed_answers:
        apply_round_ratings(round_, completed_answers)
        voting.backfill_no_reaction(round_.question, [answer.profile for answer in completed_answers])
    realtime.broadcast(round_.session_id, "round.revealed", serializers.serialize_round_reveal(round_))


def _advance_or_complete(session: TriviaSession) -> None:
    """After a round finishes, start the next one or complete the session - whichever applies."""
    next_round = get_or_create_round(session)
    if next_round is not None:
        realtime.broadcast(session.pk, "round.started", {"round": serializers.serialize_round(next_round)})
    else:
        complete_session(session)
        realtime.broadcast(session.pk, "session.completed", session_summary(session))


def force_reveal_round(round_: TriviaRound) -> None:
    """Force a stalled round to completion without waiting for every participant to answer.

    Called by the stall-sweep Celery task (``tasks.sweep_stalled_trivia_sessions``)
    for a round that's simply been open too long - the safety net for a
    participant who closed their tab mid-round, which ``submit_answer``'s
    "every joined participant answered" gate has no way to detect on its
    own. Mirrors ``services.spotguessr.session.force_reveal_round``.

    A participant who never answered this round simply scores 0 for it and
    isn't rated (see ``_finish_round``). If literally nobody answered - the
    whole table walked away - the session is marked ``ABANDONED`` instead of
    manufacturing an empty next round forever; a round with at least one
    answer instead reveals normally and advances/completes exactly like
    ``submit_answer`` would.

    Idempotent: a round already revealed (e.g. an answer completed it in the
    instant before the sweep fired) is a silent no-op.
    """
    session = round_.session
    with transaction.atomic():
        locked_round = TriviaRound.objects.select_for_update().get(pk=round_.pk)
        if locked_round.revealed_at is not None:
            return
        locked_round.revealed_at = timezone.now()
        locked_round.save(update_fields=["revealed_at", "updated"])

    completed_answers = list(TriviaAnswer.objects.for_round(locked_round).select_related("profile"))
    if not completed_answers:
        session.status = TriviaSessionStatus.ABANDONED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
        realtime.broadcast(session.pk, "session.completed", session_summary(session))
        return

    _finish_round(locked_round, completed_answers)
    _advance_or_complete(session)


def end_session_now(session: TriviaSession, host: Profile) -> TriviaSession:
    """Host-triggered manual escape hatch: end the game immediately, wherever it currently is.

    Unlike ``force_reveal_round`` (which only fires once a round's own stall
    timeout elapses), this ends the whole session on request - the host
    doesn't have to wait out a stalled/AFK player at all. Works from either
    LOBBY (cancels a game that never started) or ACTIVE. If a round is still
    open, it's revealed first using whichever answers already exist, so
    in-flight progress isn't silently dropped from the final scoreboard -
    but the session always ends as COMPLETED (never ABANDONED), since ending
    it is exactly what the host asked for. Mirrors
    ``services.spotguessr.session.end_session_now``.

    Raises:
        TriviaError: if the caller isn't the host, or the session has
            already ended.
    """
    if session.host_profile_id != host.pk:
        raise TriviaError("Only the host can end the game.")
    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        raise TriviaError("This game has already ended.")

    current_round = TriviaRound.objects.for_session(session).filter(revealed_at__isnull=True).first()
    if current_round is not None:
        with transaction.atomic():
            locked_round = TriviaRound.objects.select_for_update().get(pk=current_round.pk)
            if locked_round.revealed_at is None:
                locked_round.revealed_at = timezone.now()
                locked_round.save(update_fields=["revealed_at", "updated"])
        completed_answers = list(TriviaAnswer.objects.for_round(current_round).select_related("profile"))
        _finish_round(current_round, completed_answers)

    session.status = TriviaSessionStatus.COMPLETED
    session.ended_at = timezone.now()
    session.save(update_fields=["status", "ended_at", "updated"])
    realtime.broadcast(session.pk, "session.completed", session_summary(session))
    return session


def _remove_participant(session: TriviaSession, participant: TriviaSessionParticipant, *, reason: str) -> None:
    """Mark ``participant`` LEFT, transfer host / abandon the session if needed, and finish an in-flight round if the removal just completed it.

    Shared by ``leave_session`` and ``kick_participant`` - the two only
    differ in who's allowed to call this and the broadcast ``reason``
    ("left" vs. "kicked"). No SpotGuessr equivalent exists yet - this is
    new ground, not a port.

    - If the departing participant was the host, host is transferred to the
      earliest-joined remaining JOINED participant (an INVITED profile can
      never become host - they haven't actually joined). If nobody JOINED
      remains, the session is marked ``ABANDONED`` - the whole table left,
      the same terminal state a zero-answer stall reaches.
    - If the departure was a currently-JOINED participant and the session is
      ACTIVE, whatever round is still open is re-checked: removing the last
      holdout can complete a round exactly the way their own answer would
      have, so that path reuses ``_finish_round``/``_advance_or_complete``
      rather than leaving the round stalled until the next stall-sweep.
    """
    was_host = session.host_profile_id == participant.profile_id
    was_joined = participant.status == TriviaSessionParticipantStatus.JOINED

    participant.status = TriviaSessionParticipantStatus.LEFT
    participant.save(update_fields=["status", "updated"])

    new_host_profile_id = None
    if was_host:
        successor = session.participants.joined().exclude(pk=participant.pk).order_by("joined_at").first()
        if successor is not None:
            session.host_profile_id = successor.profile_id
            session.save(update_fields=["host_profile", "updated"])
            new_host_profile_id = successor.profile_id

    realtime.broadcast(
        session.pk,
        "participant.left",
        {"profile_id": participant.profile_id, "reason": reason, "new_host_profile_id": new_host_profile_id},
    )

    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        return

    remaining = session.participants.joined().count()
    if remaining == 0:
        session.status = TriviaSessionStatus.ABANDONED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
        realtime.broadcast(session.pk, "session.completed", session_summary(session))
        return

    if not was_joined or session.status != TriviaSessionStatus.ACTIVE:
        return

    current_round = TriviaRound.objects.for_session(session).filter(revealed_at__isnull=True).first()
    if current_round is None:
        return
    completed_answers = list(TriviaAnswer.objects.for_round(current_round).select_related("profile"))
    if len(completed_answers) < remaining:
        return  # still waiting on someone who's actually still here

    with transaction.atomic():
        locked_round = TriviaRound.objects.select_for_update().get(pk=current_round.pk)
        if locked_round.revealed_at is not None:
            return
        locked_round.revealed_at = timezone.now()
        locked_round.save(update_fields=["revealed_at", "updated"])
    _finish_round(locked_round, completed_answers)
    _advance_or_complete(session)


def leave_session(session: TriviaSession, profile: Profile) -> None:
    """``profile`` voluntarily leaves the session - or declines an invitation to it.

    Works from either ``INVITED`` or ``JOINED``: "leave" covers both a
    joined player backing out mid-game and an invitee simply declining.
    A no-op if ``profile`` already left. See ``_remove_participant`` for
    what happens next (host transfer, session abandonment, round
    completion).

    Raises:
        TriviaError: if ``profile`` isn't a participant of this session, or
            the session has already ended.
    """
    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        raise TriviaError("This game has already ended.")
    try:
        participant = TriviaSessionParticipant.objects.get(session=session, profile=profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise TriviaError("You are not part of this session.") from None
    if participant.status == TriviaSessionParticipantStatus.LEFT:
        return
    _remove_participant(session, participant, reason="left")


def kick_participant(session: TriviaSession, host: Profile, target_profile: Profile) -> None:
    """Host-only: remove another participant from the session.

    The host can't remove themselves this way (use ``end_session_now``
    instead - kicking the host would just trigger a host transfer, which
    isn't what "the host wants to end this" means). A no-op if
    ``target_profile`` already left.

    Raises:
        TriviaError: if the caller isn't the host, the target is the host
            themselves, the target isn't a participant, or the session has
            already ended.
    """
    if session.host_profile_id != host.pk:
        raise TriviaError("Only the host can remove a player.")
    if target_profile.pk == host.pk:
        raise TriviaError("The host can't remove themselves - use End game instead.")
    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        raise TriviaError("This game has already ended.")
    try:
        participant = TriviaSessionParticipant.objects.get(session=session, profile=target_profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise TriviaError("That profile is not part of this session.") from None
    if participant.status == TriviaSessionParticipantStatus.LEFT:
        return
    _remove_participant(session, participant, reason="kicked")


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
