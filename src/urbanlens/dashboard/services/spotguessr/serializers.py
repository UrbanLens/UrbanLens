"""JSON-shape builders shared by the HTTP controller and the WebSocket consumer.

Promoted out of ``controllers.spotguessr`` (Phase 1 kept these private to the
view) because UL-392's real-time broadcasts need the exact same shapes the
HTTP endpoints return - one source of truth, not two copies to keep in sync.
A round's answer is never included until it's actually revealed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.spotguessr.model import Guess, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr import street_view

if TYPE_CHECKING:
    from urbanlens.dashboard.models.spotguessr.model import (
        GameRound,
        GameSession,
        GameSessionChatMessage,
        GameSessionParticipant,
    )


def serialize_round(round_: GameRound) -> dict[str, Any]:
    """Round data safe to send before it's guessed - never the answer.

    Mode-dependent payload: Photos includes ``image_url``; Named Place
    includes the snapshotted ``display_text``; Street View re-fetches its
    (cache-backed) imagery from the location's coordinates each call.
    """
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "session_id": round_.session_id,
        "mode": round_.session.mode,
        "sequence_index": round_.sequence_index,
        "revealed": round_.revealed_at is not None,
    }
    if round_.session.mode == SpotGuessrMode.PHOTOS:
        if round_.image_id and round_.image is not None and round_.image.image:
            data["image_url"] = round_.image.image.url
            data["image_caption"] = round_.image.caption
    elif round_.session.mode == SpotGuessrMode.NAMED_PLACE:
        data["display_text"] = round_.display_text
    elif round_.session.mode == SpotGuessrMode.STREET_VIEW:
        data["street_view_image"] = street_view.candidate_street_view_for_location(round_.location)
    return data


def serialize_reveal(round_: GameRound, guess: Guess) -> dict[str, Any]:
    """One guess's own score - the HTTP response to whoever just guessed.

    The answer is only included once ``round_`` is actually revealed
    (``revealed_at`` set - every joined participant has guessed). Solo
    sessions complete the round on this very guess, so they always see it
    immediately; in multiplayer, an early guesser must not learn the
    answer before their teammates have guessed too - the session's live
    chat would otherwise let them relay it and defeat the round entirely.
    Withheld rounds are completed for the guesser by the ``round.revealed``
    broadcast (``serialize_round_reveal``) once everyone else catches up.
    """
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "distance_meters": guess.distance_meters,
        "points": guess.points,
        "date_points": guess.date_points,
        "revealed": round_.revealed_at is not None,
    }
    if round_.revealed_at is not None:
        location = round_.location
        data["actual_latitude"] = float(location.latitude)
        data["actual_longitude"] = float(location.longitude)
        data["location_name"] = location.official_name
    return data


def serialize_round_reveal(round_: GameRound) -> dict[str, Any]:
    """The answer + every participant's result - broadcast to the whole session once a round completes."""
    location = round_.location
    guesses = Guess.objects.for_round(round_).select_related("profile__user")
    return {
        "round_id": round_.pk,
        "actual_latitude": float(location.latitude),
        "actual_longitude": float(location.longitude),
        "location_name": location.official_name,
        "results": [
            {
                "profile_id": guess.profile_id,
                "username": guess.profile.user.username,
                "avatar_url": guess.profile.avatar.url if guess.profile.avatar else None,
                "distance_meters": guess.distance_meters,
                "points": guess.points,
                "date_points": guess.date_points,
            }
            for guess in guesses
        ],
    }


def serialize_participant(participant: GameSessionParticipant) -> dict[str, Any]:
    """One row of the lobby/scoreboard list."""
    return {
        "profile_id": participant.profile_id,
        "username": participant.profile.user.username,
        "avatar_url": participant.profile.avatar.url if participant.profile.avatar else None,
        "status": participant.status,
        "total_points": participant.total_points,
        "is_host": participant.session.host_profile_id == participant.profile_id,
    }


def serialize_session(session: GameSession) -> dict[str, Any]:
    """Lobby state: mode, status, and every participant (invited or joined)."""
    return {
        "session_id": session.pk,
        "mode": session.mode,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "host_profile_id": session.host_profile_id,
        "participants": [serialize_participant(participant) for participant in session.participants.select_related("profile__user")],
    }


def serialize_chat_message(message: GameSessionChatMessage) -> dict[str, Any]:
    """One chat message, for both the WebSocket broadcast and the HTTP history endpoint."""
    return {
        "message_id": message.pk,
        "profile_id": message.profile_id,
        "username": message.profile.user.username,
        "body": message.body,
        "created": message.created.isoformat(),
    }
