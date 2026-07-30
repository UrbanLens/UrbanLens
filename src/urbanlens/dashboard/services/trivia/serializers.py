"""JSON-shape builders shared by the HTTP controller and the WebSocket consumer.

Mirrors ``services.spotguessr.serializers`` - one source of truth for both
the HTTP endpoints and the real-time broadcasts, since UL-392-style
multiplayer needs the exact same shapes. A round's answer is never included
until it's actually revealed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.trivia.model import (
        TriviaAnswer,
        TriviaRound,
        TriviaSession,
        TriviaSessionChatMessage,
        TriviaSessionParticipant,
    )


def serialize_round(round_: TriviaRound) -> dict[str, Any]:
    """Round data safe to send before it's answered - never the answer."""
    return {
        "round_id": round_.pk,
        "session_id": round_.session_id,
        "sequence_index": round_.sequence_index,
        "prompt": round_.question.prompt,
        "question_id": round_.question_id,
        "revealed": round_.revealed_at is not None,
    }


def serialize_reveal(round_: TriviaRound, answer: TriviaAnswer) -> dict[str, Any]:
    """One answer's own result - the HTTP response to whoever just answered.

    The correct answer is only included once ``round_`` is actually revealed
    (every joined participant has answered). Solo sessions complete the
    round on this very answer, so they always see it immediately; in
    multiplayer, an early answerer must not learn it before their teammates
    have answered too - the session's live chat would otherwise let them
    relay it and defeat the round entirely. A withheld round is completed
    for the answerer by the ``round.revealed`` broadcast
    (``serialize_round_reveal``) once everyone else catches up.
    """
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "question_id": round_.question_id,
        "is_correct": answer.is_correct,
        "points": answer.points,
        "revealed": round_.revealed_at is not None,
    }
    if round_.revealed_at is not None:
        data["answer"] = round_.question.answer
    return data


def serialize_round_reveal(round_: TriviaRound) -> dict[str, Any]:
    """The answer + every participant's result - broadcast to the whole session once a round completes."""
    from urbanlens.dashboard.models.trivia.model import TriviaAnswer as TriviaAnswerModel

    answers = TriviaAnswerModel.objects.for_round(round_).select_related("profile__user")
    return {
        "round_id": round_.pk,
        "question_id": round_.question_id,
        "answer": round_.question.answer,
        "results": [
            {
                "profile_id": answer.profile_id,
                "username": answer.profile.user.username,
                "avatar_url": answer.profile.avatar.url if answer.profile.avatar else None,
                "is_correct": answer.is_correct,
                "points": answer.points,
            }
            for answer in answers
        ],
    }


def serialize_participant(participant: TriviaSessionParticipant) -> dict[str, Any]:
    """One row of the lobby/scoreboard list."""
    return {
        "profile_id": participant.profile_id,
        "username": participant.profile.user.username,
        "avatar_url": participant.profile.avatar.url if participant.profile.avatar else None,
        "status": participant.status,
        "total_points": participant.total_points,
        "is_host": participant.session.host_profile_id == participant.profile_id,
    }


def serialize_session(session: TriviaSession) -> dict[str, Any]:
    """Lobby state: status, and every current participant (invited or joined - never a departed one)."""
    from urbanlens.dashboard.models.trivia.model import TriviaSessionParticipantStatus

    participants = session.participants.exclude(status=TriviaSessionParticipantStatus.LEFT).select_related("profile__user")
    return {
        "session_id": session.pk,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "host_profile_id": session.host_profile_id,
        "participants": [serialize_participant(participant) for participant in participants],
    }


def serialize_chat_message(message: TriviaSessionChatMessage) -> dict[str, Any]:
    """One chat message, for both the WebSocket broadcast and the HTTP history endpoint."""
    return {
        "message_id": message.pk,
        "profile_id": message.profile_id,
        "username": message.profile.user.username,
        "body": message.body,
        "created": message.created.isoformat(),
    }
