"""Session orchestration: solo/multiplayer lifecycle, round generation, scoring guesses.

The one place ``controllers.spotguessr``/``consumers.GameSessionConsumer`` call
into - the only layer that knows how eligibility, mode-specific selection,
scoring, ratings, and real-time broadcast compose together. See
``docs/designs/spotguessr.md`` for the full rules.
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
    GameSessionParticipantStatus,
    GameSessionStatus,
    Guess,
    SpotGuessrMode,
)
from urbanlens.dashboard.services.connections import are_connections
from urbanlens.dashboard.services.spotguessr import eligibility, named_place, photos, realtime, scoring, selection, serializers, street_view
from urbanlens.dashboard.services.spotguessr.ratings import apply_round_ratings

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.profile.model import Profile

DEFAULT_ROUNDS_PER_SESSION = 5
MIN_ROUNDS_PER_SESSION = 3
MAX_ROUNDS_PER_SESSION = 20

#: How many locations to try before giving up on generating a round - guards
#: against looping forever when a whole eligible pool turns out to have no
#: usable photo/name/imagery without erroring the whole session.
_MAX_LOCATION_ATTEMPTS = 25


class SpotGuessrError(Exception):
    """Raised for invalid session/round/guess/lobby operations."""


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
    use_aliases: bool = True
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


def _clamp_rounds(total_rounds: int) -> int:
    return max(MIN_ROUNDS_PER_SESSION, min(MAX_ROUNDS_PER_SESSION, total_rounds))


def start_solo_session(profile: Profile, mode: str, config: GameConfig, *, total_rounds: int = DEFAULT_ROUNDS_PER_SESSION) -> GameSession:
    """Create a new single-participant, immediately-ACTIVE SpotGuessr session for ``profile``."""
    session = GameSession.objects.create(
        host_profile=profile,
        mode=mode,
        status=GameSessionStatus.ACTIVE,
        config=config.to_dict(),
        total_rounds=_clamp_rounds(total_rounds),
    )
    GameSessionParticipant.objects.create(session=session, profile=profile, status=GameSessionParticipantStatus.JOINED)
    return session


def start_multiplayer_session(
    host: Profile,
    mode: str,
    config: GameConfig,
    invite_profiles: Iterable[Profile],
    *,
    total_rounds: int = DEFAULT_ROUNDS_PER_SESSION,
) -> GameSession:
    """Create a LOBBY session hosted by ``host`` and invite the given (friend) profiles.

    The host's own participant row is created JOINED immediately - no
    invite step for yourself. Each invitee gets an INVITED row plus a
    notification (see ``_notify_invite``). See "Multiplayer sessions" in
    ``docs/designs/spotguessr.md`` for the full lobby lifecycle.
    """
    session = GameSession.objects.create(
        host_profile=host,
        mode=mode,
        status=GameSessionStatus.LOBBY,
        config=config.to_dict(),
        total_rounds=_clamp_rounds(total_rounds),
    )
    GameSessionParticipant.objects.create(session=session, profile=host, status=GameSessionParticipantStatus.JOINED)
    for invitee in invite_profiles:
        invite_to_session(session, host, invitee)
    return session


def invite_to_session(session: GameSession, host: Profile, invitee: Profile) -> GameSessionParticipant:
    """Invite one friend to a lobby session, notifying them.

    Host-only, friends-only (matching the trip-invite precedent) - inviting
    a non-friend is rejected server-side, not just hidden in a picker UI.

    Raises:
        SpotGuessrError: if the caller isn't the host, the session has
            already started, or ``invitee`` isn't a friend of the host.
    """
    if session.host_profile_id != host.pk:
        raise SpotGuessrError("Only the host can invite players.")
    if session.status != GameSessionStatus.LOBBY:
        raise SpotGuessrError("Can't invite once the game has started.")
    if not are_connections(host, invitee):
        raise SpotGuessrError("You can only invite friends.")

    participant, created = GameSessionParticipant.objects.get_or_create(
        session=session,
        profile=invitee,
        defaults={"status": GameSessionParticipantStatus.INVITED},
    )
    if created:
        _notify_invite(host, invitee, session)
    return participant


def _notify_invite(host: Profile, invitee: Profile, session: GameSession) -> None:
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
        notification_type=NotificationType.SPOTGUESSR_INVITE,
        title="SpotGuessr invitation",
        message=f"{host_name} invited you to play SpotGuessr.",
        # "spotguessr.lobby" is a JSON API endpoint, not a page - link to the
        # game page itself with a query param it knows to pick up on load.
        url=f"{reverse('spotguessr')}?session={session.pk}",
    )


def join_session(session: GameSession, profile: Profile) -> GameSessionParticipant:
    """Accept an invitation - flips INVITED to JOINED and broadcasts to the lobby.

    Idempotent for a profile that's already JOINED (harmless re-POST, or a
    reconnecting participant). Only actually-new joins are rejected once
    the roster is locked - not a no-op re-call from someone already in.

    Raises:
        SpotGuessrError: if ``profile`` was never invited to this session,
            or the roster is already locked (the session isn't in LOBBY)
            and they hadn't joined before that happened - an invite that
            arrived too late to act on.
    """
    try:
        participant = GameSessionParticipant.objects.get(session=session, profile=profile)
    except GameSessionParticipant.DoesNotExist:
        raise SpotGuessrError("You were not invited to this session.") from None

    if participant.status == GameSessionParticipantStatus.JOINED:
        return participant

    if session.status != GameSessionStatus.LOBBY:
        raise SpotGuessrError("This game has already started - you can no longer join.")

    participant.status = GameSessionParticipantStatus.JOINED
    participant.save(update_fields=["status", "updated"])
    realtime.broadcast(session.pk, "participant.joined", {"participant": serializers.serialize_participant(participant)})
    return participant


def begin_session(session: GameSession, host: Profile) -> GameRound | None:
    """Host starts the game: locks the roster, transitions LOBBY to ACTIVE, creates round 1.

    No one can join after this point (see "Multiplayer sessions" in the
    design doc for why mid-game joining isn't supported).

    Raises:
        SpotGuessrError: if the caller isn't the host or the session isn't
            still in its lobby.
    """
    if session.host_profile_id != host.pk:
        raise SpotGuessrError("Only the host can start the game.")
    if session.status != GameSessionStatus.LOBBY:
        raise SpotGuessrError("This session has already started.")

    session.status = GameSessionStatus.ACTIVE
    session.save(update_fields=["status", "updated"])
    round_ = get_or_create_round(session)
    if round_ is not None:
        realtime.broadcast(session.pk, "session.started", {"round": serializers.serialize_round(round_)})
    return round_


def get_or_create_round(session: GameSession) -> GameRound | None:
    """Return the session's current round, creating the next one once the prior round is fully guessed.

    Only JOINED participants count - an invitee who never accepted is not
    a player and must not gate eligibility or "has everyone guessed."

    Returns:
        The round to play/show next, or None when the session is complete
        (every configured round was played) or has run out of eligible,
        playable locations - either way, the caller should treat None as
        "call ``complete_session``."
    """
    config = _config_from_session(session)
    joined_participants = list(session.participants.joined().select_related("profile"))
    participant_count = len(joined_participants)
    if participant_count == 0:
        return None

    existing_rounds = list(GameRound.objects.for_session(session).select_related("location", "image"))
    if existing_rounds:
        last_round = existing_rounds[-1]
        if Guess.objects.for_round(last_round).count() < participant_count:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    participants = [participant.profile for participant in joined_participants]
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
        display_text = None
        if session.mode == SpotGuessrMode.PHOTOS:
            image = photos.candidate_image_for_location(location, external_media_only=config.external_media_only)
            if image is None:
                excluded_ids.append(location.pk)
                continue  # this location has no usable photo yet - try another
            target = scoring.resolve_target(location, image)
        elif session.mode == SpotGuessrMode.NAMED_PLACE:
            display_text = named_place.candidate_name_for_location(location, use_aliases=config.use_aliases)
            if display_text is None:
                excluded_ids.append(location.pk)
                continue  # no meaningful name/alias yet - try another
            target = scoring.resolve_target(location, None)
        elif session.mode == SpotGuessrMode.STREET_VIEW:
            if street_view.candidate_street_view_for_location(location) is None:
                excluded_ids.append(location.pk)
                continue  # no Street View coverage nearby - try another
            target = scoring.street_view_target(location)
        else:
            raise SpotGuessrError(f"Mode {session.mode!r} has no round-generation logic.")

        return GameRound.objects.create(
            session=session,
            sequence_index=len(existing_rounds),
            location=location,
            image=image,
            display_text=display_text,
            target_is_point=target.is_point,
            target_point=target.geometry if target.is_point else None,
        )

    return None


def submit_guess(round_: GameRound, profile: Profile, guess_point: Point, guessed_date: date | None = None) -> Guess:
    """Score and record ``profile``'s guess for ``round_``.

    Triggers the Glicko-2 rating update (``apply_round_ratings``) once
    every JOINED participant has guessed, and - for multiplayer sessions -
    broadcasts ``guess.submitted`` immediately and ``round.revealed`` +
    either ``round.started`` or ``session.completed`` once the round
    completes (see "Real-time sync" in the design doc). Solo sessions still
    work exactly as in Phase 1; broadcasting is simply a no-op without a
    channel layer listener.

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
    realtime.broadcast(session.pk, "guess.submitted", {"profile_id": profile.pk})

    joined_count = session.participants.joined().count()
    if Guess.objects.for_round(round_).count() >= joined_count:
        round_.revealed_at = timezone.now()
        round_.save(update_fields=["revealed_at", "updated"])
        apply_round_ratings(round_, list(Guess.objects.for_round(round_).select_related("profile")))
        realtime.broadcast(session.pk, "round.revealed", serializers.serialize_round_reveal(round_))

        next_round = get_or_create_round(session)
        if next_round is not None:
            realtime.broadcast(session.pk, "round.started", {"round": serializers.serialize_round(next_round)})
        else:
            complete_session(session)
            realtime.broadcast(session.pk, "session.completed", session_summary(session))

    return guess


def complete_session(session: GameSession) -> GameSession:
    """Mark a session finished (all rounds played, or no eligible locations remained)."""
    if session.status == GameSessionStatus.ACTIVE:
        session.status = GameSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
    return session


def session_summary(session: GameSession) -> dict:
    """A JSON-ready summary: rounds played and per-(joined)-participant totals."""
    rounds_played = GameRound.objects.for_session(session).count()
    participants = session.participants.joined().select_related("profile__user").order_by("-total_points")
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
