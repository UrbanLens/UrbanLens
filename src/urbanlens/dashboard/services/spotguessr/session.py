"""Session orchestration: solo/multiplayer lifecycle, round generation, scoring guesses.

The one place ``controllers.spotguessr``/``consumers.GameSessionConsumer`` call
into - the only layer that knows how eligibility, mode-specific selection,
scoring, ratings, and real-time broadcast compose together. See
``docs/designs/drafts/spotguessr.md`` for the full rules.
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

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.spotguessr.model import (
    GameRound,
    GameSession,
    GameSessionParticipant,
    GameSessionParticipantStatus,
    GameSessionStatus,
    Guess,
    SpotGuessrMode,
)
from urbanlens.dashboard.services.social.connections import are_connections
from urbanlens.dashboard.services.spotguessr import eligibility, geo_bonus, modes, photo_coordinates, prewarm, realtime, relevance, scoring, selection, serializers
from urbanlens.dashboard.services.spotguessr.ratings import RatingChange, apply_round_ratings

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.spotguessr.modes import RoundContent

DEFAULT_ROUNDS_PER_SESSION = 5
MIN_ROUNDS_PER_SESSION = 3
MAX_ROUNDS_PER_SESSION = 20

#: How many locations to try before giving up on generating a round - guards
#: against looping forever when a whole eligible pool turns out to have no
#: usable photo/name/imagery without erroring the whole session.
_MAX_LOCATION_ATTEMPTS = 25

#: Round-timer choices the settings dialog offers (seconds) - a session's
#: config only ever stores one of these or None (untimed); see GameConfig.
ROUND_TIME_LIMIT_CHOICES = (30, 60, 90, 120)

#: How long a round can sit unrevealed before the stall-sweep Celery task
#: force-reveals it - the safety net for a participant who closed their
#: tab. Independent of round_time_limit_seconds, whose expiry the client
#: reports itself while one is still around; this is the backstop for
#: when none is.
STALL_ROUND_TIMEOUT_MINUTES = 10


class SpotGuessrError(Exception):
    """Raised for invalid session/round/guess/lobby operations.

    The message is for logs, not the response: HTTP-facing callers must
    catch a specific subclass and author their own user-facing text, so a
    new ``raise`` here can never smuggle unreviewed text into a response.
    """


class NotSessionHostForInviteError(SpotGuessrError):
    """The caller isn't this session's host; only the host may invite players."""


class LobbyClosedForInviteError(SpotGuessrError):
    """The session has already left its lobby phase - it can no longer be invited into."""


class InviteeNotFriendError(SpotGuessrError):
    """The invitee isn't a friend/connection of the host."""


class ParticipantNotInvitedError(SpotGuessrError):
    """The profile has no participant row for this session at all - it was never invited."""


class LobbyClosedForJoinError(SpotGuessrError):
    """The session left its lobby phase before this invitation was accepted."""


class NotSessionHostForStartError(SpotGuessrError):
    """The caller isn't this session's host; only the host may start the game."""


class SessionAlreadyStartedError(SpotGuessrError):
    """The session has already left LOBBY - it can't be started a second time."""


class RoundGenerationUnavailableError(SpotGuessrError):
    """No round-generation strategy is registered for the session's mode.

    A configuration/programming bug (an enabled ``SpotGuessrMode`` with no
    matching entry in ``modes``), not a condition a player caused.
    """


class ParticipantNotJoinedError(SpotGuessrError):
    """The profile must be a JOINED participant of this round's session to guess in it."""


class DuplicateGuessError(SpotGuessrError):
    """This profile already submitted a guess for this round."""


class NotSessionHostForEndError(SpotGuessrError):
    """The caller isn't this session's host; only the host may end the game."""


class SessionAlreadyEndedError(SpotGuessrError):
    """The session isn't LOBBY or ACTIVE any more - it has already ended."""


@dataclass(frozen=True)
class GameConfig:
    """A validated, session-ready snapshot of SpotGuessr settings.

    Mirrors what's stored on ``GameSession.config`` - see
    ``docs/designs/drafts/spotguessr.md``'s config table for defaults.
    """

    difficulty: float = 0.5
    allow_arbitrary_external_photos: bool = False
    require_visited_all: bool = False
    date_guessing_enabled: bool = False
    use_aliases: bool = True
    geo_bounds_geojson: dict | None = None
    #: Seconds each round stays open before it's force-revealed, or None for
    #: untimed play. One of ROUND_TIME_LIMIT_CHOICES in practice (the
    #: settings dialog only offers those), but not validated against that
    #: list here - any positive int is honored.
    round_time_limit_seconds: int | None = None
    #: Restrict candidate locations to those tagged (by any participant's own pin)
    #: with this Label or one of its descendants - see
    #: ``services.spotguessr.eligibility.eligible_locations``'s ``label_id`` for the
    #: exact semantics and why it can never surface a location nobody in the
    #: session has pinned.
    label_id: int | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for ``GameSession.config``."""
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


def config_from_session(session: GameSession) -> GameConfig:
    """Reconstruct a GameConfig from a session's stored config snapshot, ignoring unknown keys."""
    known_fields = {f.name for f in dataclasses.fields(GameConfig)}
    return GameConfig(**{key: value for key, value in (session.config or {}).items() if key in known_fields})


def clamp_rounds(total_rounds: int) -> int:
    """Coerce a requested round count into the range a session may actually play.

    Public so every caller with a client-supplied round count shares one
    clamp - a second copy of ``max(MIN, min(MAX, n))`` is how the bounds
    would eventually disagree.

    Args:
        total_rounds: The requested number of rounds, from any source.

    Returns:
        The same number pinned into
        ``[MIN_ROUNDS_PER_SESSION, MAX_ROUNDS_PER_SESSION]``.
    """
    return max(MIN_ROUNDS_PER_SESSION, min(MAX_ROUNDS_PER_SESSION, total_rounds))


def start_solo_session(profile: Profile, mode: str, config: GameConfig, *, total_rounds: int = DEFAULT_ROUNDS_PER_SESSION) -> GameSession:
    """Create a new single-participant, immediately-ACTIVE SpotGuessr session for ``profile``."""
    session = GameSession.objects.create(
        host_profile=profile,
        mode=mode,
        status=GameSessionStatus.ACTIVE,
        config=config.to_dict(),
        total_rounds=clamp_rounds(total_rounds),
    )
    GameSessionParticipant.objects.create(session=session, profile=profile, status=GameSessionParticipantStatus.JOINED)
    return session


@dataclass(frozen=True)
class SoloStartResult:
    """The outcome of :func:`start_solo_playthrough`.

    Attributes:
        session: The created session, or None when nothing was created.
        round: The first round to show, or None when the session was
            abandoned before it played anything.
        no_eligible_locations: True when the profile has nothing playable
            under this config. Report as its own empty state, not as a
            completed game with zero rounds.
    """

    session: GameSession | None
    round: GameRound | None
    no_eligible_locations: bool


def start_solo_playthrough(profile: Profile, mode: str, config: GameConfig, *, total_rounds: int = DEFAULT_ROUNDS_PER_SESSION) -> SoloStartResult:
    """Start a solo session and generate its first round, in one call.

    One shared "create, check it can play, clean up if it can't" sequence:
    getting it subtly wrong strands an ACTIVE session that can never
    produce a round. A profile with nothing eligible gets no ``GameSession``
    at all; one whose candidates all fail generation gets a session that
    is completed immediately so it can't linger.

    Args:
        profile: The solo player.
        mode: The ``SpotGuessrMode`` to play.
        config: The validated settings snapshot for this playthrough.
        total_rounds: Requested round count; clamped by :func:`clamp_rounds`.

    Returns:
        A :class:`SoloStartResult` - see its attributes for how to tell the
        three outcomes apart.

    Raises:
        RoundGenerationUnavailableError: If the mode has no round-generation
            strategy registered (a programming error, not a player-facing
            condition).
    """
    if not eligibility.has_eligible_locations(
        [profile],
        require_visited_by_all=config.require_visited_all,
        geo_bounds=config.geo_bounds,
        label_id=config.label_id,
    ):
        return SoloStartResult(session=None, round=None, no_eligible_locations=True)

    session = start_solo_session(profile, mode, config, total_rounds=total_rounds)
    round_ = get_or_create_round(session)
    if round_ is None:
        # The pre-check only ruled out "nothing pinned at all". Reaching
        # None here means every eligible location failed generation, so
        # complete the session rather than leaving it ACTIVE and unplayable.
        complete_session(session)
        return SoloStartResult(session=session, round=None, no_eligible_locations=True)

    return SoloStartResult(session=session, round=round_, no_eligible_locations=False)


def start_multiplayer_session(
    host: Profile,
    mode: str,
    config: GameConfig,
    invite_profiles: Iterable[Profile],
    *,
    total_rounds: int = DEFAULT_ROUNDS_PER_SESSION,
) -> GameSession:
    """Create a LOBBY session hosted by ``host`` and invite the given (friend) profiles.

    The host joins immediately; each invitee gets an INVITED row plus a
    notification. See "Multiplayer sessions" in the design doc for the
    full lobby lifecycle.
    """
    session = GameSession.objects.create(
        host_profile=host,
        mode=mode,
        status=GameSessionStatus.LOBBY,
        config=config.to_dict(),
        total_rounds=clamp_rounds(total_rounds),
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
        NotSessionHostForInviteError: if the caller isn't the host.
        LobbyClosedForInviteError: if the session has already started.
        InviteeNotFriendError: if ``invitee`` isn't a friend of the host.
    """
    if session.host_profile_id != host.pk:
        raise NotSessionHostForInviteError(f"Profile {host.pk} is not host {session.host_profile_id} of session {session.pk}.")
    if session.status != GameSessionStatus.LOBBY:
        raise LobbyClosedForInviteError(f"Session {session.pk} is {session.status}, not LOBBY.")
    if not are_connections(host, invitee):
        raise InviteeNotFriendError(f"Profile {invitee.pk} is not a connection of host {host.pk}.")

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
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    host_name = resolve_visible_identity(invitee, host)["display_name"]
    NotificationLog.objects.notify(
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

    Idempotent for an already-JOINED profile. Only actually-new joins are
    rejected once the roster is locked.

    Raises:
        ParticipantNotInvitedError: if ``profile`` was never invited to this
            session.
        LobbyClosedForJoinError: if the roster is already locked and they
            hadn't joined before that happened.
    """
    try:
        participant = GameSessionParticipant.objects.get(session=session, profile=profile)
    except GameSessionParticipant.DoesNotExist:
        raise ParticipantNotInvitedError(f"Profile {profile.pk} has no participant row for session {session.pk}.") from None

    if participant.status == GameSessionParticipantStatus.JOINED:
        return participant

    if session.status != GameSessionStatus.LOBBY:
        raise LobbyClosedForJoinError(f"Session {session.pk} is {session.status}, not LOBBY; profile {profile.pk} was never accepted.")

    participant.status = GameSessionParticipantStatus.JOINED
    participant.save(update_fields=["status", "updated"])
    realtime.broadcast(session.pk, "participant.joined", {"participant": serializers.serialize_participant(participant)})
    return participant


def begin_session(session: GameSession, host: Profile) -> GameRound | None:
    """Host starts the game: locks the roster, transitions LOBBY to ACTIVE, creates round 1.

    No one can join after this point (see "Multiplayer sessions" in the
    design doc).

    Raises:
        NotSessionHostForStartError: if the caller isn't the host.
        SessionAlreadyStartedError: if the session isn't still in its lobby.
    """
    if session.host_profile_id != host.pk:
        raise NotSessionHostForStartError(f"Profile {host.pk} is not host {session.host_profile_id} of session {session.pk}.")
    if session.status != GameSessionStatus.LOBBY:
        raise SessionAlreadyStartedError(f"Session {session.pk} is {session.status}, not LOBBY.")

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
        or has run out of playable locations - treat None as "call
        ``complete_session``."
    """
    config = config_from_session(session)
    joined_participants = list(session.participants.joined().select_related("profile"))
    participant_count = len(joined_participants)
    if participant_count == 0:
        return None

    existing_rounds = list(GameRound.objects.for_session(session).select_related("location", "image"))
    if existing_rounds:
        last_round = existing_rounds[-1]
        # revealed_at is the single source of truth for "is this round
        # done" - a stalled/timed-out round can be revealed with only a
        # subset of guesses, where the guess count alone would re-serve
        # this finished round forever.
        if last_round.revealed_at is None:
            return last_round

    if len(existing_rounds) >= session.total_rounds:
        return None

    participants = [participant.profile for participant in joined_participants]
    excluded_ids = [round_.location_id for round_ in existing_rounds]
    previous_location = existing_rounds[-1].location if existing_rounds else None

    # Bonus eligibility is computed once, from the full eligible pool: the
    # joined roster is only known here, and freezing it now stops later
    # rounds (which exclude used locations) from spuriously narrowing it.
    if not existing_rounds and "bonus_scope" not in (session.config or {}):
        initial_candidates = eligibility.eligible_locations(
            participants,
            require_visited_by_all=config.require_visited_all,
            geo_bounds=config.geo_bounds,
            label_id=config.label_id,
        )
        scope = geo_bonus.bonus_scope_for(initial_candidates)
        session.config = {**(session.config or {}), "bonus_scope": scope.to_dict()}
        session.save(update_fields=["config", "updated"])

    if modes.get_strategy(session.mode) is None:
        raise RoundGenerationUnavailableError(f"Session {session.pk}: mode {session.mode!r} has no round-generation strategy registered.")

    next_sequence_index = len(existing_rounds)
    picked = _consume_prewarmed_pick(session, config, participants, next_sequence_index, excluded_ids)
    if picked is None:
        picked = generate_round_content(session.mode, config, participants, excluded_ids, previous_location)
    if picked is None:
        return None
    location, content = picked

    new_round = GameRound.objects.create(
        session=session,
        sequence_index=next_sequence_index,
        location=location,
        image=content.image,
        display_text=content.display_text,
        target_is_point=content.target.is_point,
        target_point=content.target.geometry if content.target.is_point else None,
    )

    # Prewarm the *next* round's selection in the background while this one
    # is played. Best-effort: on a broker hiccup the next round just falls
    # back to live generation.
    if next_sequence_index + 1 < session.total_rounds:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import prewarm_spotguessr_round

        safely_enqueue_task(prewarm_spotguessr_round, session.pk, next_sequence_index + 1)

    return new_round


def _consume_prewarmed_pick(
    session: GameSession,
    config: GameConfig,
    participants: list[Profile],
    sequence_index: int,
    excluded_ids: list[int],
) -> tuple[Location, RoundContent] | None:
    """Redeem a background-prewarmed round for this exact round, if one is cached and still valid.

    Tries the session-scoped prewarm first, then (solo first rounds only)
    the speculative one queued from the overview page. A stale pick is a
    miss, not an error - the caller falls back to live generation.
    """
    cached = prewarm.consume_for_session(session.pk, sequence_index)
    if cached is None and sequence_index == 0 and len(participants) == 1 and participants[0].pk == session.host_profile_id:
        cached = prewarm.consume_for_solo_start(session.host_profile_id, session.mode, config)
    if cached is None:
        return None

    location_id, content = cached
    if location_id in excluded_ids:
        return None
    try:
        location = Location.objects.get(pk=location_id)
    except Location.DoesNotExist:
        return None
    return location, content


def generate_round_content(
    mode: str,
    config: GameConfig,
    participants: list[Profile],
    excluded_location_ids: list[int],
    previous_location: Location | None,
) -> tuple[Location, RoundContent] | None:
    """Pick a location and build this mode's round content for it, retrying past unusable candidates.

    Shared with the background prewarm tasks, which must select by identical
    rules to the request path. Read-only: creating the ``GameRound`` row is
    the caller's job.

    Args:
        mode: The ``SpotGuessrMode`` to generate a round for.
        config: The session's settings.
        participants: Every profile whose eligibility must agree.
        excluded_location_ids: Locations to rule out (already used this
            session, or already tried and found to have nothing usable).
        previous_location: The prior round's location, for anti-clustering -
            see ``selection.pick_next_location``.

    Returns:
        ``(location, content)``, or None if every eligible location (up to
        ``_MAX_LOCATION_ATTEMPTS`` of them) turned out to have nothing usable
        for this mode, or none were eligible at all.

    Raises:
        RoundGenerationUnavailableError: If ``mode`` has no round-generation
            strategy registered (a programming error, not a player-facing
            condition).
    """
    strategy = modes.get_strategy(mode)
    if strategy is None:
        raise RoundGenerationUnavailableError(f"Mode {mode!r} has no round-generation strategy registered.")

    excluded_ids = list(excluded_location_ids)
    # Resolved once, not per attempt: eligibility is the most expensive
    # query on the game path and nothing it depends on changes between
    # attempts. The per-attempt queryset stays a plain pk filter so
    # pick_next_location's PostGIS proximity filter keeps working.
    eligible_ids = list(
        eligibility.eligible_locations(
            participants,
            require_visited_by_all=config.require_visited_all,
            geo_bounds=config.geo_bounds,
            exclude_location_ids=excluded_ids,
            label_id=config.label_id,
        ).values_list("pk", flat=True),
    )

    if mode == SpotGuessrMode.PHOTOS:
        # Same "resolved once" reasoning: without this, a photo-less profile
        # makes the loop pay one Image query per location attempted.
        from urbanlens.dashboard.services.spotguessr import photos

        solo_profile = participants[0] if len(participants) == 1 else None
        eligible_ids = photos.locations_with_eligible_photo(eligible_ids, solo_profile=solo_profile)

    for _attempt in range(_MAX_LOCATION_ATTEMPTS):
        candidates = Location.objects.filter(pk__in=eligible_ids).exclude(pk__in=excluded_ids)
        location = selection.pick_next_location(candidates, mode=mode, difficulty=config.difficulty, previous_location=previous_location)
        if location is None:
            return None  # nothing eligible left at all

        content = strategy.build_round(location, config, participants)
        if content is None:
            excluded_ids.append(location.pk)
            continue  # this location has nothing usable for this mode yet - try another

        return location, content

    return None


def submit_guess(round_: GameRound, profile: Profile, guess_point: Point, guessed_date: date | None = None) -> tuple[Guess, list[str], RatingChange | None]:
    """Score and record ``profile``'s guess for ``round_``.

    Triggers the Glicko-2 rating update (``apply_round_ratings``) once
    every JOINED participant has guessed, and - for multiplayer sessions -
    broadcasts ``guess.submitted`` immediately and ``round.revealed`` +
    either ``round.started`` or ``session.completed`` once the round
    completes (see "Real-time sync" in the design doc). Solo sessions still
    work exactly as in Phase 1; broadcasting is simply a no-op without a
    channel layer listener.

    Returns:
        The saved ``Guess``, the matched bonus tiers for the reveal response
        (not persisted - only available here), and ``profile``'s Glicko-2
        rating change if this guess completed the round (None otherwise).

    Raises:
        ParticipantNotJoinedError: if ``profile`` isn't a JOINED participant
            of this round's session (e.g. still INVITED, never joined).
        DuplicateGuessError: if ``profile`` already guessed this round.
    """
    try:
        participant = GameSessionParticipant.objects.get(session=round_.session, profile=profile)
    except GameSessionParticipant.DoesNotExist:
        raise ParticipantNotJoinedError(f"Profile {profile.pk} has no participant row for session {round_.session_id}.") from None
    if participant.status != GameSessionParticipantStatus.JOINED:
        raise ParticipantNotJoinedError(f"Profile {profile.pk} is {participant.status}, not JOINED, in session {round_.session_id}.")

    distance = scoring.distance_for_guess(
        round_.location,
        guess_point,
        target_is_point=round_.target_is_point,
        target_point=round_.target_point,
    )
    points = scoring.points_for_distance(distance)

    session = round_.session
    config = config_from_session(session)
    date_points = 0
    if config.date_guessing_enabled and guessed_date is not None and round_.image is not None and round_.image.taken_at is not None:
        date_points = scoring.points_for_date_guess(guessed_date, round_.image.taken_at.date())

    bonus_scope = geo_bonus.BonusScope.from_dict((session.config or {}).get("bonus_scope", {}))
    bonus = geo_bonus.bonus_points_for_guess(guess_point, round_.location, bonus_scope)

    # Two round-completing guesses can land at the same instant, which would
    # double-apply ratings and race the next round's uniqueness.
    # select_for_update() serializes the read-count-decide section per round;
    # duplicates then surface as IntegrityError, and only the submission that
    # actually completes the round proceeds to rating/broadcast/next-round.
    round_completed_now = False
    with transaction.atomic():
        locked_round = GameRound.objects.select_for_update().get(pk=round_.pk)
        try:
            guess = Guess.objects.create(
                round=locked_round,
                profile=profile,
                guess_point=guess_point,
                distance_meters=distance,
                points=points,
                guessed_date=guessed_date,
                date_points=date_points,
                bonus_points=bonus.total,
            )
        except IntegrityError:
            raise DuplicateGuessError(f"Profile {profile.pk} already has a guess recorded for round {locked_round.pk}.") from None

        # Recorded only once the guess is confirmed genuinely new - a retried
        # guess for an answered round must not write coordinate evidence
        # that the fact-sourcing pipeline would read as real signal.
        photo_coordinates.record_guess(locked_round, guess_point, distance)

        GameSessionParticipant.objects.filter(session=session, profile=profile).update(total_points=F("total_points") + points + date_points + bonus.total)

        joined_count = session.participants.joined().count()
        if locked_round.revealed_at is None and Guess.objects.for_round(locked_round).count() >= joined_count:
            locked_round.revealed_at = timezone.now()
            locked_round.save(update_fields=["revealed_at", "updated"])
            round_completed_now = True

    realtime.broadcast(session.pk, "guess.submitted", {"profile_id": profile.pk})

    rating_change = None
    if round_completed_now:
        round_.refresh_from_db()
        completed_guesses = list(Guess.objects.for_round(round_).select_related("profile"))
        rating_changes = _finish_round(round_, completed_guesses)
        rating_change = rating_changes.get(profile.pk)
        _advance_or_complete(session)

    return guess, bonus.matched_tiers, rating_change


def _finish_round(round_: GameRound, completed_guesses: list[Guess]) -> dict[int, RatingChange]:
    """Rate, backfill photo-feedback signal, and broadcast the reveal for a just-completed round.

    Shared by the guess/timer/stall-sweep/host-end paths, so
    ``completed_guesses`` may be a strict subset of the roster or empty. A
    participant with no guess isn't rated; the reveal still broadcasts, so
    "nobody answered in time" reads as an empty round, not a stall.
    """
    rating_changes: dict[int, RatingChange] = {}
    if completed_guesses:
        rating_changes = apply_round_ratings(round_, completed_guesses)
        for guess in completed_guesses:
            change = rating_changes.get(guess.profile_id)
            if change is not None:
                GameSessionParticipant.objects.filter(session_id=round_.session_id, profile_id=guess.profile_id).update(
                    rating_delta=F("rating_delta") + change.delta,
                )
        relevance.backfill_no_reaction(round_, [guess.profile for guess in completed_guesses])
    realtime.broadcast(round_.session_id, "round.revealed", serializers.serialize_round_reveal(round_, rating_changes))
    return rating_changes


def _advance_or_complete(session: GameSession) -> None:
    """After a round finishes, start the next one or complete the session - whichever applies."""
    next_round = get_or_create_round(session)
    if next_round is not None:
        realtime.broadcast(session.pk, "round.started", {"round": serializers.serialize_round(next_round)})
    else:
        complete_session(session)
        realtime.broadcast(session.pk, "session.completed", session_summary(session))


def force_reveal_round(round_: GameRound) -> None:
    """Force a stalled round to completion without waiting for every participant to guess.

    Called by the stall-sweep Celery task for a round open too long - the
    safety net for a participant who closed their tab, which the "everyone
    guessed" gate can't detect. See ``expire_round_timer`` for the
    never-abandons timer-expiry path.

    A participant who never guessed scores 0 and isn't rated. With zero
    guesses the session is marked ``ABANDONED``; otherwise the round reveals
    and advances exactly like ``submit_guess``.

    Idempotent: an already-revealed round is a silent no-op.
    """
    session = round_.session
    with transaction.atomic():
        locked_round = GameRound.objects.select_for_update().get(pk=round_.pk)
        if locked_round.revealed_at is not None:
            return
        locked_round.revealed_at = timezone.now()
        locked_round.save(update_fields=["revealed_at", "updated"])

    completed_guesses = list(Guess.objects.for_round(locked_round).select_related("profile"))
    if not completed_guesses:
        session.status = GameSessionStatus.ABANDONED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
        realtime.broadcast(session.pk, "session.completed", session_summary(session))
        return

    _finish_round(locked_round, completed_guesses)
    _advance_or_complete(session)


def expire_round_timer(round_: GameRound) -> None:
    """Reveal a round because its configured round-timer ran out - "time's up," not a stall.

    Gentler than ``force_reveal_round``: an expired timer is a normal
    gameplay outcome, so this never marks the session ``ABANDONED`` (a
    genuinely dead session still gets caught by the stall sweep).

    Idempotent: an already-revealed round is a silent no-op.
    """
    session = round_.session
    with transaction.atomic():
        locked_round = GameRound.objects.select_for_update().get(pk=round_.pk)
        if locked_round.revealed_at is not None:
            return
        locked_round.revealed_at = timezone.now()
        locked_round.save(update_fields=["revealed_at", "updated"])

    completed_guesses = list(Guess.objects.for_round(locked_round).select_related("profile"))
    _finish_round(locked_round, completed_guesses)
    _advance_or_complete(session)


def end_session_now(session: GameSession, host: Profile) -> GameSession:
    """Host-triggered manual escape hatch: end the game immediately, wherever it currently is.

    Ends the whole session on request, from LOBBY or ACTIVE - the host never
    waits out a stalled player. An open round is revealed first from existing
    guesses so in-flight progress isn't dropped; the session always ends as
    COMPLETED (never ABANDONED).

    Raises:
        NotSessionHostForEndError: if the caller isn't the host.
        SessionAlreadyEndedError: if the session has already ended.
    """
    if session.host_profile_id != host.pk:
        raise NotSessionHostForEndError(f"Profile {host.pk} is not host {session.host_profile_id} of session {session.pk}.")
    if session.status not in (GameSessionStatus.LOBBY, GameSessionStatus.ACTIVE):
        raise SessionAlreadyEndedError(f"Session {session.pk} is {session.status}, already ended.")

    current_round = GameRound.objects.for_session(session).filter(revealed_at__isnull=True).first()
    if current_round is not None:
        with transaction.atomic():
            locked_round = GameRound.objects.select_for_update().get(pk=current_round.pk)
            if locked_round.revealed_at is None:
                locked_round.revealed_at = timezone.now()
                locked_round.save(update_fields=["revealed_at", "updated"])
        completed_guesses = list(Guess.objects.for_round(current_round).select_related("profile"))
        _finish_round(current_round, completed_guesses)

    session.status = GameSessionStatus.COMPLETED
    session.ended_at = timezone.now()
    session.save(update_fields=["status", "ended_at", "updated"])
    realtime.broadcast(session.pk, "session.completed", session_summary(session))
    return session


def rounds_played(session: GameSession) -> int:
    """How many rounds this session has ever created.

    Lets callers tell "finished after playing" apart from "never played a
    single round" when ``get_or_create_round`` returns None.
    """
    return GameRound.objects.for_session(session).count()


def complete_session(session: GameSession) -> GameSession:
    """Mark a session finished (all rounds played, or no eligible locations remained)."""
    if session.status == GameSessionStatus.ACTIVE:
        session.status = GameSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at", "updated"])
    return session


def session_summary(session: GameSession) -> dict:
    """A JSON-ready summary: rounds played, per-participant totals, and each
    participant's net rating change and best round this session.
    """
    participants = session.participants.joined().select_related("profile__user").order_by("-total_points")

    guesses_by_profile: dict[int, list[Guess]] = {}
    for guess in Guess.objects.filter(round__session=session).only("profile_id", "points", "date_points", "bonus_points", "distance_meters"):
        guesses_by_profile.setdefault(guess.profile_id, []).append(guess)

    def _best_guess(profile_id: int) -> Guess | None:
        guesses = guesses_by_profile.get(profile_id)
        if not guesses:
            return None
        return max(guesses, key=lambda guess: guess.points + guess.date_points + guess.bonus_points)

    rows = []
    for participant in participants:
        best = _best_guess(participant.profile_id)
        rows.append(
            {
                "profile_id": participant.profile_id,
                "username": participant.profile.user.username,
                "avatar_url": participant.profile.avatar.url if participant.profile.avatar else None,
                "total_points": participant.total_points,
                "rating_delta": round(participant.rating_delta, 1),
                "best_round_points": (best.points + best.date_points + best.bonus_points) if best else None,
                "best_round_distance_meters": best.distance_meters if best else None,
            },
        )

    return {
        "session_id": session.pk,
        "mode": session.mode,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "rounds_played": rounds_played(session),
        "participants": rows,
    }
