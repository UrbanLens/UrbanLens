"""Session orchestration: solo/competitive lifecycle, round generation, answer/vote resolution.

The one place ``controllers.consensus``/``consumers.ConsensusSessionConsumer``
call into - mirrors ``services.spotguessr.session`` closely, but with three
extra branches unique to Consensus: trust-check rounds (never touch the
wiki), ``WIKI_ALIAS`` rounds (additive, never routed through agree/vote), and
the vote-then-tentative disagreement path (see ``_finish_round``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from urbanlens.dashboard.models.consensus.model import (
    ConsensusAnswer,
    ConsensusFieldKind,
    ConsensusRound,
    ConsensusRoundResolution,
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionParticipantStatus,
    ConsensusSessionStatus,
    ConsensusVote,
)
from urbanlens.dashboard.services.consensus import eligibility, fields, points, realtime, selection, serializers, tentative, trust, voting

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.profile.model import Profile

DEFAULT_ROUNDS_PER_SESSION = 5
MIN_ROUNDS_PER_SESSION = 3
MAX_ROUNDS_PER_SESSION = 20

#: How long a session's current round (answer-collection or vote) can sit
#: unresolved before the stall-sweep Celery task
#: (``tasks.sweep_stalled_consensus_sessions``) force-resolves it - the
#: safety net for a participant who simply closed their tab. Mirrors
#: SpotGuessr's ``STALL_ROUND_TIMEOUT_MINUTES``.
STALL_ROUND_TIMEOUT_MINUTES = 10


class ConsensusError(Exception):
    """Raised for invalid session/round/answer/vote/lobby operations."""


def _clamp_rounds(total_rounds: int) -> int:
    return max(MIN_ROUNDS_PER_SESSION, min(MAX_ROUNDS_PER_SESSION, total_rounds))


def start_solo_session(profile: Profile, *, total_rounds: int = DEFAULT_ROUNDS_PER_SESSION) -> ConsensusSession:
    """Create a new single-participant, immediately-ACTIVE Consensus session for ``profile``."""
    session = ConsensusSession.objects.create(
        host_profile=profile,
        status=ConsensusSessionStatus.ACTIVE,
        total_rounds=_clamp_rounds(total_rounds),
    )
    ConsensusSessionParticipant.objects.create(session=session, profile=profile, status=ConsensusSessionParticipantStatus.JOINED)
    return session


def start_competitive_session(
    host: Profile,
    invite_profiles: Iterable[Profile],
    *,
    total_rounds: int = DEFAULT_ROUNDS_PER_SESSION,
    vote_threshold: float = ConsensusSession.DEFAULT_VOTE_THRESHOLD,
) -> ConsensusSession:
    """Create a LOBBY session hosted by ``host`` and invite the given (friend) profiles."""
    session = ConsensusSession.objects.create(
        host_profile=host,
        status=ConsensusSessionStatus.LOBBY,
        config={"vote_threshold": vote_threshold},
        total_rounds=_clamp_rounds(total_rounds),
    )
    ConsensusSessionParticipant.objects.create(session=session, profile=host, status=ConsensusSessionParticipantStatus.JOINED)
    for invitee in invite_profiles:
        invite_to_session(session, host, invitee)
    return session


def invite_to_session(session: ConsensusSession, host: Profile, invitee: Profile) -> ConsensusSessionParticipant:
    """Invite one friend to a lobby session, notifying them. Host-only, friends-only.

    Raises:
        ConsensusError: if the caller isn't the host, the session has
            already started, or ``invitee`` isn't a friend of the host.
    """
    from urbanlens.dashboard.services.connections import are_connections

    if session.host_profile_id != host.pk:
        raise ConsensusError("Only the host can invite players.")
    if session.status != ConsensusSessionStatus.LOBBY:
        raise ConsensusError("Can't invite once the game has started.")
    if not are_connections(host, invitee):
        raise ConsensusError("You can only invite friends.")

    participant, created = ConsensusSessionParticipant.objects.get_or_create(
        session=session,
        profile=invitee,
        defaults={"status": ConsensusSessionParticipantStatus.INVITED},
    )
    if created:
        _notify_invite(host, invitee, session)
    return participant


def _notify_invite(host: Profile, invitee: Profile, session: ConsensusSession) -> None:
    """Create the in-app (+ live toast, via the existing NotificationLog signal) invite notification."""
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity

    host_name = resolve_visible_identity(invitee, host)["display_name"]
    NotificationLog.objects.create(
        profile=invitee,
        source_profile=host,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.CONSENSUS_INVITE,
        title="Consensus invitation",
        message=f"{host_name} invited you to play Consensus.",
        url=f"{reverse('consensus')}?session={session.pk}",
    )


def join_session(session: ConsensusSession, profile: Profile) -> ConsensusSessionParticipant:
    """Accept an invitation - flips INVITED to JOINED and broadcasts to the lobby.

    Raises:
        ConsensusError: if ``profile`` was never invited, or the roster is
            already locked (the session isn't in LOBBY) and they hadn't
            joined before that happened.
    """
    try:
        participant = ConsensusSessionParticipant.objects.get(session=session, profile=profile)
    except ConsensusSessionParticipant.DoesNotExist:
        raise ConsensusError("You were not invited to this session.") from None

    if participant.status == ConsensusSessionParticipantStatus.JOINED:
        return participant
    if session.status != ConsensusSessionStatus.LOBBY:
        raise ConsensusError("This game has already started - you can no longer join.")

    participant.status = ConsensusSessionParticipantStatus.JOINED
    participant.save(update_fields=["status", "updated"])
    realtime.broadcast(session.pk, "participant.joined", {"participant": serializers.serialize_participant(participant)})
    return participant


def begin_session(session: ConsensusSession, host: Profile) -> ConsensusRound | None:
    """Host starts the game: locks the roster, transitions LOBBY to ACTIVE, creates round 1.

    Raises:
        ConsensusError: if the caller isn't the host or the session isn't
            still in its lobby.
    """
    if session.host_profile_id != host.pk:
        raise ConsensusError("Only the host can start the game.")
    if session.status != ConsensusSessionStatus.LOBBY:
        raise ConsensusError("This session has already started.")

    session.status = ConsensusSessionStatus.ACTIVE
    session.save(update_fields=["status", "updated"])
    round_ = get_or_create_round(session)
    if round_ is not None:
        realtime.broadcast(session.pk, "session.started", {"round": serializers.serialize_round(round_)})
    return round_


def _wrap_known_value(value: Any) -> dict[str, Any]:
    return {"value": value}


def _unwrap_known_value(round_: ConsensusRound) -> Any | None:
    if not round_.known_answer_snapshot:
        return None
    return round_.known_answer_snapshot.get("value")


def _known_value_for_comparison(round_: ConsensusRound) -> Any | None:
    """The round's snapshotted known answer, in the same comparable form ``strategy.agrees`` expects."""
    raw = _unwrap_known_value(round_)
    if raw is None:
        return None
    if round_.field_kind == ConsensusFieldKind.PHOTO_COORDINATES:
        from django.contrib.gis.geos import Point

        return Point(raw["longitude"], raw["latitude"], srid=4326)
    return raw


def get_or_create_round(session: ConsensusSession) -> ConsensusRound | None:
    """Return the session's current round, creating the next one once the prior round has fully settled.

    Only JOINED participants count. Returns None once every configured
    round was played, or the eligible pool has nothing left usable - the
    caller should treat None as "call ``complete_session``."
    """
    joined_participants = list(session.participants.joined().select_related("profile"))
    if not joined_participants:
        return None

    existing_rounds = list(ConsensusRound.objects.for_session(session).select_related("wiki__location", "target_image"))
    if existing_rounds:
        last_round = existing_rounds[-1]
        # `resolution` is the single source of truth for "is this round
        # done" - PENDING/VOTE_OPEN both mean "still in progress."
        if not last_round.is_settled:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    profiles = [participant.profile for participant in joined_participants]
    excluded_ids = [round_.wiki_id for round_ in existing_rounds]

    selection_result = selection.pick_next_round_content(profiles, exclude_wiki_ids=excluded_ids)
    if selection_result is None:
        return None

    return ConsensusRound.objects.create(
        session=session,
        sequence_index=len(existing_rounds),
        wiki=selection_result.wiki,
        field_kind=selection_result.field_kind,
        target_image=selection_result.content.target_image,
        is_check_round=selection_result.is_check_round,
        known_answer_snapshot=_wrap_known_value(selection_result.known_value) if selection_result.is_check_round else None,
    )


def _get_joined_participant(session: ConsensusSession, profile: Profile) -> ConsensusSessionParticipant:
    try:
        participant = ConsensusSessionParticipant.objects.get(session=session, profile=profile)
    except ConsensusSessionParticipant.DoesNotExist:
        raise ConsensusError("You must join this session before playing.") from None
    if participant.status != ConsensusSessionParticipantStatus.JOINED:
        raise ConsensusError("You must join this session before playing.")
    return participant


def submit_answer(round_: ConsensusRound, profile: Profile, value: str | Point | None) -> ConsensusAnswer:
    """Submit (or, with ``value=None``, skip) ``profile``'s answer for ``round_``.

    Args:
        round_: The round being answered.
        profile: Who's answering - must be a JOINED participant.
        value: The submitted value (a string for every field kind except
            ``PHOTO_COORDINATES``, a ``Point`` for that one), or None to
            skip - a skip still counts toward "has everyone in this round
            responded," but never contributes to agreement/voting and never
            earns points.

    Raises:
        ConsensusError: if ``profile`` isn't a JOINED participant, this
            round has already settled, or ``profile`` already answered it.
    """
    _get_joined_participant(round_.session, profile)
    strategy = fields.get_strategy(round_.field_kind)
    if strategy is None:
        raise ConsensusError(f"Field kind {round_.field_kind!r} has no strategy.")

    text_value = None
    normalized_text = None
    guess_point = None
    if value is not None:
        if round_.field_kind == ConsensusFieldKind.PHOTO_COORDINATES:
            guess_point = value
        else:
            text_value = str(value)
            normalized_text = strategy.normalize(value)

    session = round_.session
    joined_count = session.participants.joined().count()
    round_completed_now = False
    with transaction.atomic():
        locked_round = ConsensusRound.objects.select_for_update().get(pk=round_.pk)
        if locked_round.is_settled:
            raise ConsensusError("This round has already been resolved.")
        try:
            answer = ConsensusAnswer.objects.create(
                round=locked_round,
                profile=profile,
                text_value=text_value,
                normalized_text=normalized_text,
                guess_point=guess_point,
            )
        except IntegrityError:
            raise ConsensusError("You've already answered this round.") from None

        if ConsensusAnswer.objects.for_round(locked_round).count() >= joined_count:
            round_completed_now = True

    realtime.broadcast(session.pk, "answer.submitted", {"profile_id": profile.pk})

    if round_completed_now:
        locked_round.refresh_from_db()
        answers = list(ConsensusAnswer.objects.for_round(locked_round).select_related("profile__user"))
        _finish_round(locked_round, answers)
        if locked_round.is_settled:
            _advance_or_complete(session)

    return answer


def skip_round(round_: ConsensusRound, profile: Profile) -> ConsensusAnswer:
    """Skip this round - "I don't know" - equivalent to ``submit_answer(round_, profile, None)``."""
    return submit_answer(round_, profile, None)


def _has_value(answer: ConsensusAnswer) -> bool:
    return bool(answer.text_value) or answer.guess_point is not None


def _answer_value(answer: ConsensusAnswer):
    return answer.guess_point if answer.guess_point is not None else answer.text_value


def _mark_settled(round_: ConsensusRound, resolution: str) -> None:
    round_.resolution = resolution
    round_.revealed_at = timezone.now()
    round_.save(update_fields=["resolution", "revealed_at", "updated"])


def _award_and_record(round_: ConsensusRound, answer: ConsensusAnswer, amount: int, *, reason: str) -> None:
    from django.db.models import F

    answer.points_awarded = amount
    answer.save(update_fields=["points_awarded", "updated"])
    points.award_points(answer.profile_id, amount, reason=reason)
    ConsensusSessionParticipant.objects.filter(session_id=round_.session_id, profile_id=answer.profile_id).update(
        total_points_this_session=F("total_points_this_session") + amount,
    )


def _apply_and_record_edit(round_: ConsensusRound, strategy, value, editor: Profile) -> None:
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

    changes = strategy.apply_answer(round_.wiki, value, editor, round_)
    if changes is not None:
        WikiEdit.objects.create(wiki=round_.wiki, editor=editor, changes=changes, consensus_round=round_)


def _resolve_check_round(round_: ConsensusRound, real_answers: list[ConsensusAnswer], strategy, participant_count: int) -> None:
    """Score a trust-check round - never touches the wiki, and always pays out as if it were a real round.

    Points are unaffected by correctness (a failed check only costs trust,
    per the design spec) so the reward loop gives players no signal that
    this round was any different from an ordinary one.
    """
    known_value = _known_value_for_comparison(round_)
    reward = points.SOLO_ANSWER_POINTS if participant_count <= 1 else points.COMPETITIVE_AGREE_POINTS
    any_correct = False
    any_incorrect = False
    for answer in real_answers:
        correct = known_value is not None and strategy.agrees(_answer_value(answer), known_value)
        answer.is_correct_for_check = correct
        _award_and_record(round_, answer, reward, reason="check_round")
        trust.record_check_result(answer.profile, correct=correct)
        if correct:
            any_correct = True
        else:
            any_incorrect = True

    if not real_answers:
        _mark_settled(round_, ConsensusRoundResolution.SKIPPED)
    elif any_incorrect and not any_correct:
        _mark_settled(round_, ConsensusRoundResolution.CHECK_FAILED)
    elif any_correct and not any_incorrect:
        _mark_settled(round_, ConsensusRoundResolution.CHECK_PASSED)
    else:
        _mark_settled(round_, ConsensusRoundResolution.CHECK_FAILED)


def _resolve_alias_round(round_: ConsensusRound, real_answers: list[ConsensusAnswer], strategy, participant_count: int) -> None:
    """Aliases are additive - every distinct proposal is added directly, never routed through agree/vote."""
    if not real_answers:
        _mark_settled(round_, ConsensusRoundResolution.SKIPPED)
        return

    reward = points.SOLO_ANSWER_POINTS if participant_count <= 1 else points.COMPETITIVE_AGREE_POINTS
    for answer in real_answers:
        _apply_and_record_edit(round_, strategy, _answer_value(answer), answer.profile)
        _award_and_record(round_, answer, reward, reason="alias_contribution")
    _mark_settled(round_, ConsensusRoundResolution.AGREED)


def _resolve_solo_round(round_: ConsensusRound, real_answers: list[ConsensusAnswer], strategy) -> None:
    if not real_answers:
        _mark_settled(round_, ConsensusRoundResolution.SKIPPED)
        return

    answer = real_answers[0]
    _apply_and_record_edit(round_, strategy, _answer_value(answer), answer.profile)
    _award_and_record(round_, answer, points.SOLO_ANSWER_POINTS, reason="solo_answer")
    _mark_settled(round_, ConsensusRoundResolution.AGREED)


def _resolve_competitive_round(round_: ConsensusRound, real_answers: list[ConsensusAnswer], strategy) -> None:
    if not real_answers:
        _mark_settled(round_, ConsensusRoundResolution.SKIPPED)
        return

    clusters = voting.cluster_answers(strategy, real_answers)
    if len(clusters) == 1:
        winning_answer = clusters[0][0]
        _apply_and_record_edit(round_, strategy, _answer_value(winning_answer), winning_answer.profile)
        for answer in real_answers:
            _award_and_record(round_, answer, points.COMPETITIVE_AGREE_POINTS, reason="competitive_agree")
        _mark_settled(round_, ConsensusRoundResolution.AGREED)
        return

    # Disagreement - open a vote instead of resolving immediately. Points
    # are withheld until `resolve_vote` runs (see submit_vote).
    voting.open_vote(round_)


def _finish_round(round_: ConsensusRound, answers: list[ConsensusAnswer]) -> None:
    """Resolve a round whose answer-collection phase just completed (every joined participant responded).

    Dispatches to whichever resolution branch applies - trust-check (never
    touches the wiki), alias (additive), solo (single answer applies
    immediately), or competitive (agree immediately, or open a vote on
    disagreement, see ``_resolve_competitive_round``). Broadcasts
    ``round.revealed`` regardless of which branch ran, even when the round
    is still ``VOTE_OPEN`` afterwards - participants need to see the
    distinct candidates to vote on them.
    """
    strategy = fields.get_strategy(round_.field_kind)
    session = round_.session
    participant_count = session.participants.joined().count()
    real_answers = [answer for answer in answers if _has_value(answer)]

    if round_.is_check_round:
        _resolve_check_round(round_, real_answers, strategy, participant_count)
    elif round_.field_kind == ConsensusFieldKind.WIKI_ALIAS:
        _resolve_alias_round(round_, real_answers, strategy, participant_count)
    elif participant_count <= 1:
        _resolve_solo_round(round_, real_answers, strategy)
    else:
        _resolve_competitive_round(round_, real_answers, strategy)

    realtime.broadcast(session.pk, "round.revealed", serializers.serialize_round_reveal(round_))


def submit_vote(round_: ConsensusRound, profile: Profile, chosen_answer: ConsensusAnswer) -> ConsensusVote:
    """Cast a vote during a competitive round's disagreement sub-phase, resolving it once everyone has voted.

    Raises:
        ConsensusError: if ``profile`` isn't a JOINED participant, the round
            isn't in its vote sub-phase, ``chosen_answer`` isn't part of
            this round, or ``profile`` already voted this round.
    """
    _get_joined_participant(round_.session, profile)
    session = round_.session
    joined_count = session.participants.joined().count()
    vote_completed_now = False
    with transaction.atomic():
        locked_round = ConsensusRound.objects.select_for_update().get(pk=round_.pk)
        if locked_round.resolution != ConsensusRoundResolution.VOTE_OPEN:
            raise ConsensusError("This round isn't open for voting.")
        try:
            vote = voting.record_vote(locked_round, profile, chosen_answer)
        except voting.ConsensusVotingError as exc:
            raise ConsensusError(str(exc)) from None

        if ConsensusVote.objects.for_round(locked_round).count() >= joined_count:
            vote_completed_now = True

    realtime.broadcast(session.pk, "vote.submitted", {"profile_id": profile.pk})

    if vote_completed_now:
        locked_round.refresh_from_db()
        resolve_vote(locked_round)

    return vote


def resolve_vote(round_: ConsensusRound) -> None:
    """Tally a round's votes and resolve its disagreement sub-phase - winner-take-more, or tentative.

    Idempotent: a round no longer ``VOTE_OPEN`` (e.g. resolved by a
    concurrent caller) is a silent no-op.
    """
    if round_.resolution != ConsensusRoundResolution.VOTE_OPEN:
        return

    session = round_.session
    strategy = fields.get_strategy(round_.field_kind)
    real_answers = [answer for answer in round_.answers.select_related("profile") if _has_value(answer)]
    tally = voting.tally_votes(round_, vote_threshold=session.vote_threshold)

    if tally.consensus_reached:
        winning_answer = tally.winning_answers[0]
        _apply_and_record_edit(round_, strategy, _answer_value(winning_answer), winning_answer.profile)
        winner_ids = {answer.pk for answer in tally.winning_answers}
        for answer in real_answers:
            if answer.pk in winner_ids:
                _award_and_record(round_, answer, points.VOTE_WINNER_POINTS, reason="vote_winner")
            else:
                _award_and_record(round_, answer, points.VOTE_PARTICIPANT_POINTS, reason="vote_participant")
        _mark_settled(round_, ConsensusRoundResolution.VOTE_RESOLVED)
    else:
        distinct_answers = [cluster[0] for cluster in voting.cluster_answers(strategy, real_answers)]
        tentative.record_tentative_answers(round_, distinct_answers)
        for answer in real_answers:
            _award_and_record(round_, answer, points.TENTATIVE_POINTS, reason="tentative")
        _mark_settled(round_, ConsensusRoundResolution.TENTATIVE)

    realtime.broadcast(session.pk, "round.revealed", serializers.serialize_round_reveal(round_))
    _advance_or_complete(session)


def _advance_or_complete(session: ConsensusSession) -> None:
    """After a round settles, start the next one or complete the session - whichever applies."""
    next_round = get_or_create_round(session)
    if next_round is not None:
        realtime.broadcast(session.pk, "round.started", {"round": serializers.serialize_round(next_round)})
    else:
        complete_session(session)
        realtime.broadcast(session.pk, "session.completed", session_summary(session))


def force_reveal_round(round_: ConsensusRound) -> None:
    """Force a stalled answer-collection round to completion without waiting for every participant.

    Called by the stall-sweep Celery task
    (``tasks.sweep_stalled_consensus_sessions``). If literally nobody
    answered or skipped (zero ``ConsensusAnswer`` rows at all - the whole
    table walked away), the session is marked ``ABANDONED`` instead of
    manufacturing an empty next round forever.

    Idempotent: a round no longer ``PENDING`` is a silent no-op.
    """
    session = round_.session
    with transaction.atomic():
        locked_round = ConsensusRound.objects.select_for_update().get(pk=round_.pk)
        if locked_round.resolution != ConsensusRoundResolution.PENDING:
            return
        answers = list(ConsensusAnswer.objects.for_round(locked_round).select_related("profile__user"))

    if not answers:
        session.status = ConsensusSessionStatus.ABANDONED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
        realtime.broadcast(session.pk, "session.completed", session_summary(session))
        return

    _finish_round(locked_round, answers)
    if locked_round.is_settled:
        _advance_or_complete(session)


def force_resolve_vote(round_: ConsensusRound) -> None:
    """Force a stalled vote sub-phase to resolve without waiting for every participant to vote.

    Called by the stall-sweep Celery task. A silent no-op if the round
    isn't (or is no longer) ``VOTE_OPEN``.
    """
    resolve_vote(round_)


def end_session_now(session: ConsensusSession, host: Profile) -> ConsensusSession:
    """Host-triggered manual escape hatch: end the game immediately, wherever it currently is.

    Raises:
        ConsensusError: if the caller isn't the host, or the session has
            already ended.
    """
    if session.host_profile_id != host.pk:
        raise ConsensusError("Only the host can end the game.")
    if session.status not in (ConsensusSessionStatus.LOBBY, ConsensusSessionStatus.ACTIVE):
        raise ConsensusError("This game has already ended.")

    current_round = ConsensusRound.objects.for_session(session).filter(resolution=ConsensusRoundResolution.PENDING).first()
    if current_round is not None:
        force_reveal_round(current_round)
    else:
        open_vote_round = ConsensusRound.objects.for_session(session).filter(resolution=ConsensusRoundResolution.VOTE_OPEN).first()
        if open_vote_round is not None:
            resolve_vote(open_vote_round)

    session.refresh_from_db()
    if session.status not in (ConsensusSessionStatus.COMPLETED, ConsensusSessionStatus.ABANDONED):
        session.status = ConsensusSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
        realtime.broadcast(session.pk, "session.completed", session_summary(session))
    return session


def rounds_played(session: ConsensusSession) -> int:
    """How many rounds this session has ever created."""
    return ConsensusRound.objects.for_session(session).count()


def complete_session(session: ConsensusSession) -> ConsensusSession:
    """Mark a session finished (all rounds played, or no eligible wikis remained)."""
    if session.status == ConsensusSessionStatus.ACTIVE:
        session.status = ConsensusSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
    return session


def session_summary(session: ConsensusSession) -> dict:
    """A JSON-ready summary: rounds played and each (joined) participant's session point total."""
    participants = session.participants.joined().select_related("profile__user").order_by("-total_points_this_session")
    return {
        "session_id": session.pk,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "rounds_played": rounds_played(session),
        "participants": [serializers.serialize_participant(participant) for participant in participants],
    }


def has_eligible_wikis(profiles: Iterable[Profile]) -> bool:
    """Cheap pre-check: would ``get_or_create_round`` have anything to work with for these profiles?"""
    profiles = list(profiles)
    if len(profiles) == 1:
        return eligibility.has_eligible_wikis(profiles[0])
    return eligibility.has_eligible_wikis_for_all(profiles)
