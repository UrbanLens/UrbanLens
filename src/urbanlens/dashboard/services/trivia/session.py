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

#: Flat points for a correct answer - a Trivia answer is binary right/wrong,
#: so there is no closeness curve and no partial credit.
POINTS_FOR_CORRECT_ANSWER = 1000


class TriviaError(Exception):
    """Raised for an invalid Trivia session/round/answer operation.

    The message is for logs, not the response: HTTP-facing callers must
    catch a subclass (or this base class) and author their own user-facing
    text, so a new ``raise`` here can never smuggle text into a response.
    """


class InviteNotHostError(TriviaError):
    """The caller isn't this session's host, and only the host may invite players."""


class InviteAfterLobbyClosedError(TriviaError):
    """The session has left LOBBY, so no more invites can go out."""


class InviteeNotFriendError(TriviaError):
    """The invitee isn't a connection of the host - only friends may be invited."""


class NotInvitedError(TriviaError):
    """The profile has no participant row for this session - it was never invited."""


class JoinAfterLobbyClosedError(TriviaError):
    """The roster locked (the session left LOBBY) before this profile joined."""


class BeginNotHostError(TriviaError):
    """The caller isn't this session's host, and only the host may begin the game."""


class SessionAlreadyBegunError(TriviaError):
    """The session has already left LOBBY, so it can't be begun a second time."""


class NotJoinedParticipantError(TriviaError):
    """The profile isn't a JOINED participant of this round's session."""


class DuplicateAnswerError(TriviaError):
    """This profile already submitted an answer for this round."""


class EndSessionNotHostError(TriviaError):
    """The caller isn't this session's host, and only the host may end the game."""


class SessionAlreadyEndedError(TriviaError):
    """The session is neither LOBBY nor ACTIVE - it has already ended."""


class NotASessionParticipantError(TriviaError):
    """The calling profile has no participant row for this session."""


class KickNotHostError(TriviaError):
    """The caller isn't this session's host, and only the host may remove a player."""


class CannotKickHostError(TriviaError):
    """The kick target is the host themselves - use ``end_session_now`` instead."""


class TargetNotAParticipantError(TriviaError):
    """The kick target has no participant row for this session."""


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

        Split at the antimeridian here, not per query: callers run planar
        ``__within`` lookups, so an area drawn across the date line would
        otherwise match nothing on its far side.

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

    The host joins immediately; each invitee gets an INVITED row plus a
    notification. Mirrors ``spotguessr.session.start_multiplayer_session``.
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
        InviteNotHostError: ``host`` isn't this session's host.
        InviteAfterLobbyClosedError: The session has already left LOBBY.
        InviteeNotFriendError: ``invitee`` isn't a connection of ``host``.
    """
    if session.host_profile_id != host.pk:
        raise InviteNotHostError("Caller is not the session host; only the host may invite players.")
    if session.status != TriviaSessionStatus.LOBBY:
        raise InviteAfterLobbyClosedError("Session is no longer in LOBBY status; invites are closed once play begins.")
    if not are_connections(host, invitee):
        raise InviteeNotFriendError("Invitee is not a connection (friend) of the host; only friends may be invited.")

    participant, created = TriviaSessionParticipant.objects.get_or_create(
        session=session,
        profile=invitee,
        defaults={"status": TriviaSessionParticipantStatus.INVITED},
    )
    if created:
        _notify_invite(host, invitee, session)
    elif participant.status == TriviaSessionParticipantStatus.LEFT:
        # LEFT is terminal - get_or_create would otherwise return the dead
        # row unchanged instead of actually re-inviting them.
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
        NotInvitedError: ``profile`` was never invited to this session.
        JoinAfterLobbyClosedError: The roster is already locked (the
            session isn't in LOBBY) and ``profile`` hadn't joined before
            that happened.
    """
    try:
        participant = TriviaSessionParticipant.objects.get(session=session, profile=profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise NotInvitedError("No TriviaSessionParticipant row exists for this profile on this session; it was never invited.") from None

    if participant.status == TriviaSessionParticipantStatus.JOINED:
        return participant

    if session.status != TriviaSessionStatus.LOBBY:
        raise JoinAfterLobbyClosedError("Session left LOBBY before this profile joined; the roster is locked.")

    participant.status = TriviaSessionParticipantStatus.JOINED
    participant.save(update_fields=["status", "updated"])
    realtime.broadcast(session.pk, "participant.joined", {"participant": serializers.serialize_participant(participant)})
    return participant


def begin_session(session: TriviaSession, host: Profile) -> TriviaRound | None:
    """Host starts the game: locks the roster, transitions LOBBY to ACTIVE, creates round 1.

    Group eligibility is only checkable once the roster locks. If nothing is
    eligible, the session stays ACTIVE (it never played) - report
    ``{"finished": false, "no_eligible_questions": true}``.

    Raises:
        BeginNotHostError: The caller isn't this session's host.
        SessionAlreadyBegunError: The session isn't still in its lobby.
    """
    if session.host_profile_id != host.pk:
        raise BeginNotHostError("Caller is not the session host; only the host may begin the game.")
    if session.status != TriviaSessionStatus.LOBBY:
        raise SessionAlreadyBegunError("Session is not in LOBBY status; it has already begun.")

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
        # A revealed round is finished no matter how many answered it - the
        # answer count alone would re-serve a force-revealed round forever,
        # stalling the session.
        if last_round.revealed_at is None and TriviaAnswer.objects.for_round(last_round).count() < participant_count:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    participants = [participant.profile for participant in joined_participants]
    excluded_question_ids = [round_.question_id for round_ in existing_rounds]

    candidates = list(eligibility.eligible_questions(participants, geo_bounds=config.geo_bounds, exclude_question_ids=excluded_question_ids))

    # Rarely, a solo player may see their own not-yet-approved question -
    # never anyone else's. See solo_own_pending_questions's docstring.
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

    Rates and backfills ``NO_REACTION`` votes once everyone answered, then
    advances (mirrors ``spotguessr.session.submit_guess``, including the
    broadcast sequence). A normalized-string mismatch falls back to
    ``answer_check`` (``SiteFeature.AI``-gated; without it, exact-match
    only - never blocked from playing).

    Raises:
        NotJoinedParticipantError: ``profile`` isn't a JOINED participant
            of this round's session (e.g. still INVITED, never joined).
        DuplicateAnswerError: ``profile`` already answered this round.
    """
    try:
        participant = TriviaSessionParticipant.objects.get(session=round_.session, profile=profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise NotJoinedParticipantError("Profile is not a JOINED participant of this round's session.") from None
    if participant.status != TriviaSessionParticipantStatus.JOINED:
        raise NotJoinedParticipantError("Profile is not a JOINED participant of this round's session.")

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
            raise DuplicateAnswerError("Profile has already submitted an answer for this round (unique constraint violation).") from None

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

    Shared by the answer/stall-sweep/host-end/removal paths, so
    ``completed_answers`` may be a subset of the roster or empty. A
    participant with no answer isn't rated; the reveal still broadcasts, so
    "nobody answered in time" reads as an empty round, not a stall.
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

    Called by the stall-sweep Celery task for a round open too long - the
    safety net for a participant who closed their tab. Mirrors
    ``spotguessr.session.force_reveal_round``.

    A participant who never answered scores 0 and isn't rated. With zero
    answers the session is marked ``ABANDONED``; otherwise the round reveals
    and advances exactly like ``submit_answer``.

    Idempotent: an already-revealed round is a silent no-op.
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

    Ends the whole session on request, from LOBBY or ACTIVE - the host never
    waits out a stalled player. An open round is revealed first from existing
    answers; the session always ends as COMPLETED (never ABANDONED). Mirrors
    ``spotguessr.session.end_session_now``.

    Raises:
        EndSessionNotHostError: The caller isn't this session's host.
        SessionAlreadyEndedError: The session has already ended.
    """
    if session.host_profile_id != host.pk:
        raise EndSessionNotHostError("Caller is not the session host; only the host may end the game.")
    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        raise SessionAlreadyEndedError("Session status is neither LOBBY nor ACTIVE; it has already ended.")

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
    """Mark ``participant`` LEFT, transfer host / abandon if needed, and finish an in-flight round if the removal just completed it.

    Shared by ``leave_session`` and ``kick_participant`` (they differ only in
    caller and broadcast ``reason``).

    - Host departure transfers host to the earliest-joined remaining JOINED
      participant; with nobody JOINED left the session is ``ABANDONED``.
    - Removing the last holdout on an ACTIVE round can complete it exactly
      like their answer would, so that path reuses ``_finish_round`` /
      ``_advance_or_complete`` instead of waiting for the stall sweep.
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
        SessionAlreadyEndedError: The session has already ended.
        NotASessionParticipantError: ``profile`` isn't a participant of
            this session.
    """
    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        raise SessionAlreadyEndedError("Session status is neither LOBBY nor ACTIVE; it has already ended.")
    try:
        participant = TriviaSessionParticipant.objects.get(session=session, profile=profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise NotASessionParticipantError("No TriviaSessionParticipant row exists for this profile on this session.") from None
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
        KickNotHostError: The caller isn't this session's host.
        CannotKickHostError: ``target_profile`` is the host themselves.
        SessionAlreadyEndedError: The session has already ended.
        TargetNotAParticipantError: ``target_profile`` isn't a participant
            of this session.
    """
    if session.host_profile_id != host.pk:
        raise KickNotHostError("Caller is not the session host; only the host may remove a player.")
    if target_profile.pk == host.pk:
        raise CannotKickHostError("Kick target is the session host; use end_session_now to end the game instead.")
    if session.status not in (TriviaSessionStatus.LOBBY, TriviaSessionStatus.ACTIVE):
        raise SessionAlreadyEndedError("Session status is neither LOBBY nor ACTIVE; it has already ended.")
    try:
        participant = TriviaSessionParticipant.objects.get(session=session, profile=target_profile)
    except TriviaSessionParticipant.DoesNotExist:
        raise TargetNotAParticipantError("No TriviaSessionParticipant row exists for the target profile on this session.") from None
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
