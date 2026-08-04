"""JSON-shape builders shared by the HTTP controller and the WebSocket consumer.

Promoted out of ``controllers.spotguessr`` (Phase 1 kept these private to the
view) because UL-392's real-time broadcasts need the exact same shapes the
HTTP endpoints return - one source of truth, not two copies to keep in sync.
A round's answer is never included until it's actually revealed - except
Street View mode's panorama coordinates, a deliberate, scoped exception (see
``services.spotguessr.street_view.StreetViewPanorama``'s docstring and
docs/designs/drafts/spotguessr.md's "Interactive panorama" note).
"""

from __future__ import annotations

from datetime import timedelta
import json
from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import GEOSGeometry

from urbanlens.dashboard.models.spotguessr.model import Guess
from urbanlens.dashboard.services.spotguessr import modes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.spotguessr.model import (
        GameRound,
        GameSession,
        GameSessionChatMessage,
        GameSessionParticipant,
    )
    from urbanlens.dashboard.services.spotguessr.ratings import RatingChange


def _geo_bounds_bbox(session: GameSession) -> list[list[float]] | None:
    """The session's configured ``geo_bounds``, as a Leaflet-ready ``[[south, west], [north, east]]`` bbox.

    Lets the frontend zoom the guess map to the configured area (see
    ``docs/designs/spotguessr.md``'s eligibility rule 3) instead of always
    opening on a fixed default view. None when no area was configured.
    """
    geo_bounds_geojson = (session.config or {}).get("geo_bounds_geojson")
    if not geo_bounds_geojson:
        return None
    xmin, ymin, xmax, ymax = GEOSGeometry(json.dumps(geo_bounds_geojson)).extent
    return [[ymin, xmin], [ymax, xmax]]


def serialize_round(round_: GameRound) -> dict[str, Any]:
    """Round data safe to send before it's guessed - never the answer, except Street View mode.

    Mode-dependent payload (``ModeStrategy.serialize_round`` per
    ``services.spotguessr.modes``): Photos includes ``image_url``; Named
    Place includes the snapshotted ``display_text``; Street View re-fetches
    its (cache-backed) imagery from the location's coordinates each call,
    including the panorama's own ``street_view_lat``/``street_view_lng`` - a
    deliberate exception to "never the answer" (see
    ``services.spotguessr.street_view.StreetViewPanorama``'s docstring for why
    a real interactive panorama can't be built any other way).
    ``shows_imagery`` tells the frontend whether this round has real visual
    content worth reacting to (see ``services.spotguessr.relevance``) - one
    server-computed field instead of the frontend hardcoding its own copy of
    "which modes show a photo".
    """
    session = round_.session
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "session_id": round_.session_id,
        "mode": session.mode,
        "sequence_index": round_.sequence_index,
        "revealed": round_.revealed_at is not None,
        "geo_bounds": _geo_bounds_bbox(session),
        "shows_imagery": modes.shows_imagery(session.mode),
        "expires_at": _round_expires_at(round_),
    }
    strategy = modes.get_strategy(session.mode)
    if strategy is not None:
        strategy.serialize_round(round_, data)
    return data


def _round_expires_at(round_: GameRound) -> str | None:
    """When this round's configured timer runs out, ISO-formatted - None for untimed play.

    Computed from ``round_.created`` (no dedicated column needed) - see
    ``GameConfig.round_time_limit_seconds`` and
    ``controllers.spotguessr.SpotGuessrRoundTimeoutView`` for who reads this.
    """
    time_limit = (round_.session.config or {}).get("round_time_limit_seconds")
    if not time_limit:
        return None
    return (round_.created + timedelta(seconds=time_limit)).isoformat()


def serialize_reveal(round_: GameRound, guess: Guess, bonus_tiers: Sequence[str] = (), rating_change: RatingChange | None = None) -> dict[str, Any]:
    """One guess's own score - the HTTP response to whoever just guessed.

    The answer is only included once ``round_`` is actually revealed
    (``revealed_at`` set - every joined participant has guessed). Solo
    sessions complete the round on this very guess, so they always see it
    immediately; in multiplayer, an early guesser must not learn the
    answer before their teammates have guessed too - the session's live
    chat would otherwise let them relay it and defeat the round entirely.
    Withheld rounds are completed for the guesser by the ``round.revealed``
    broadcast (``serialize_round_reveal``) once everyone else catches up.

    Args:
        round_: The round just guessed.
        guess: The saved guess.
        bonus_tiers: Which country/state/city tiers this guess matched (see
            ``services.spotguessr.session.submit_guess``'s return value) -
            not persisted on ``Guess``, so only available right here.
        rating_change: This guesser's own Glicko-2 rating change, if the
            round completed on this very guess (see
            ``services.spotguessr.session.submit_guess``'s return value) -
            None when the reveal is still withheld, or a rating couldn't be
            computed for some other reason.
    """
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "distance_meters": guess.distance_meters,
        "points": guess.points,
        "date_points": guess.date_points,
        "bonus_points": guess.bonus_points,
        "bonus_tiers": list(bonus_tiers),
        "revealed": round_.revealed_at is not None,
        "rating_delta": round(rating_change.delta, 1) if rating_change is not None else None,
    }
    if round_.revealed_at is not None:
        location = round_.location
        data["actual_latitude"] = float(location.latitude)
        data["actual_longitude"] = float(location.longitude)
        data["location_name"] = location.official_name
        # Deliberately here rather than on the pre-reveal round payload: a
        # photo's caption is EXIF/IPTC-derived and frequently names the place,
        # which is a free answer before the guess and merely context after it.
        data["image_caption"] = _image_caption(round_)
    return data


def _image_caption(round_: GameRound) -> str | None:
    """The caption of the photo this round showed, or None when there wasn't one.

    Args:
        round_: The round whose photo to describe.

    Returns:
        The stored caption, or None for a round with no image (Named Place,
        Street View) or an image that never had one.
    """
    if round_.image_id and round_.image is not None:
        return round_.image.caption or None
    return None


def serialize_round_reveal(round_: GameRound, rating_changes: dict[int, RatingChange] | None = None) -> dict[str, Any]:
    """The answer + every participant's result - broadcast to the whole session once a round completes.

    Args:
        round_: The just-completed round.
        rating_changes: Each guesser's own Glicko-2 rating change from this
            round (``services.spotguessr.ratings.apply_round_ratings``'s
            return value), keyed by profile id - None (or a missing key, for
            a force-revealed round's non-guessing participants) renders as no
            ``rating_delta`` for that result.
    """
    location = round_.location
    guesses = Guess.objects.for_round(round_).select_related("profile__user")
    rating_changes = rating_changes or {}
    return {
        "round_id": round_.pk,
        "actual_latitude": float(location.latitude),
        "actual_longitude": float(location.longitude),
        "location_name": location.official_name,
        # See serialize_reveal: post-reveal only, because the caption tends to
        # name the place and would otherwise be a pre-guess giveaway.
        "image_caption": _image_caption(round_),
        "results": [
            {
                "profile_id": guess.profile_id,
                "username": guess.profile.user.username,
                "avatar_url": guess.profile.avatar.url if guess.profile.avatar else None,
                "distance_meters": guess.distance_meters,
                "points": guess.points,
                "date_points": guess.date_points,
                "bonus_points": guess.bonus_points,
                "rating_delta": round(change.delta, 1) if (change := rating_changes.get(guess.profile_id)) is not None else None,
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
        "geo_bounds": _geo_bounds_bbox(session),
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
