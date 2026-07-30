"""JSON-shape builders shared by the HTTP controller and the WebSocket consumer.

Mirrors ``services.spotguessr.serializers`` - one source of truth for both
the HTTP endpoints and the real-time broadcasts. A round's outbound payload
never differs between an ordinary round and a trust-check round (see
``models.consensus.model.ConsensusRound.is_check_round``'s docstring) -
that parity is load-bearing for the whole trust mechanism, so
``serialize_round`` deliberately has no branch on it at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind, ConsensusRoundResolution
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.consensus.model import (
        ConsensusAnswer,
        ConsensusProfile,
        ConsensusRound,
        ConsensusSession,
        ConsensusSessionChatMessage,
        ConsensusSessionParticipant,
    )


def _answer_value(answer: ConsensusAnswer) -> Any:
    """The submitted value in display form - text as-is, coordinates as a lat/lng dict."""
    if answer.guess_point is not None:
        return {"latitude": answer.guess_point.y, "longitude": answer.guess_point.x}
    return answer.text_value


def serialize_round(round_: ConsensusRound) -> dict[str, Any]:
    """Round data safe to send before it's answered - never the (possibly already-known) answer.

    Deliberately identical in shape whether or not ``round_.is_check_round``
    is set - see this module's docstring.
    """
    wiki = round_.wiki
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "session_id": round_.session_id,
        "sequence_index": round_.sequence_index,
        "field_kind": round_.field_kind,
        "field_label": ConsensusFieldKind(round_.field_kind).label,
        "wiki_id": wiki.pk,
        "wiki_name": wiki.name if is_meaningful_name(wiki.name) else "Unnamed location",
        "latitude": float(wiki.latitude) if wiki.latitude is not None else None,
        "longitude": float(wiki.longitude) if wiki.longitude is not None else None,
        "resolved": round_.is_settled,
    }
    if round_.field_kind == ConsensusFieldKind.PHOTO_COORDINATES and round_.target_image is not None and round_.target_image.image:
        data["image_url"] = round_.target_image.image.url
    return data


def serialize_answer(answer: ConsensusAnswer) -> dict[str, Any]:
    """One participant's submitted answer, for the post-reveal results list."""
    return {
        "profile_id": answer.profile_id,
        "username": answer.profile.user.username,
        "avatar_url": answer.profile.avatar.url if answer.profile.avatar else None,
        "value": _answer_value(answer),
        "points_awarded": answer.points_awarded,
    }


def serialize_round_reveal(round_: ConsensusRound) -> dict[str, Any]:
    """The outcome + every participant's result - broadcast/returned once a round settles.

    ``vote_options`` is only populated while ``resolution`` is
    ``VOTE_OPEN`` - the distinct candidate values participants can vote for,
    each carrying the ``ConsensusAnswer`` id a vote must reference.
    """
    answers = round_.answers.select_related("profile__user").order_by("submitted_at")
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "resolution": round_.resolution,
        "answers": [serialize_answer(answer) for answer in answers],
    }
    if round_.resolution == ConsensusRoundResolution.VOTE_OPEN:
        data["vote_options"] = [{"answer_id": answer.pk, "value": _answer_value(answer), "submitted_by": answer.profile.user.username} for answer in answers]
    return data


def serialize_participant(participant: ConsensusSessionParticipant) -> dict[str, Any]:
    """One row of the lobby/scoreboard list."""
    return {
        "profile_id": participant.profile_id,
        "username": participant.profile.user.username,
        "avatar_url": participant.profile.avatar.url if participant.profile.avatar else None,
        "status": participant.status,
        "total_points_this_session": participant.total_points_this_session,
        "is_host": participant.session.host_profile_id == participant.profile_id,
    }


def serialize_session(session: ConsensusSession) -> dict[str, Any]:
    """Lobby state: status, and every participant (invited or joined)."""
    return {
        "session_id": session.pk,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "host_profile_id": session.host_profile_id,
        "participants": [serialize_participant(participant) for participant in session.participants.select_related("profile__user")],
    }


def serialize_chat_message(message: ConsensusSessionChatMessage) -> dict[str, Any]:
    """One chat message, for both the WebSocket broadcast and the HTTP history endpoint."""
    return {
        "message_id": message.pk,
        "profile_id": message.profile_id,
        "username": message.profile.user.username,
        "body": message.body,
        "created": message.created.isoformat(),
    }


def serialize_consensus_profile(consensus_profile: ConsensusProfile) -> dict[str, Any]:
    """A profile's points/level summary - deliberately excludes the trust score, which is never user-facing."""
    from urbanlens.dashboard.services.consensus.points import points_required_for_level

    return {
        "total_points": consensus_profile.total_points,
        "level": consensus_profile.level,
        "points_for_next_level": points_required_for_level(consensus_profile.level),
    }
