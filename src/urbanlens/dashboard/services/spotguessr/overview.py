"""What a client needs to render "the SpotGuessr screen you land on".

The web overview page (``controllers.spotguessr.SpotGuessrHomeView``) and the
external API's ``games/spotguessr/`` endpoint answer the same three questions -
what are this player's saved settings, what is their current rating, and is
there a game already in progress to resume - and they must answer them
identically. Before this module the answers were computed inline in the view,
so the API would have had to restate each of them; two restatements of "which
rating counts as *your* rating" is exactly how the homepage chip came to read
the wrong row in the first place (it was hardcoded to Photos mode, so a
Named Place-only player saw nothing at all).

Deliberately holds nothing presentational: mode cards, icons, and reversed URL
templates stay in the web controller, because they describe how one particular
client draws the screen rather than what the screen is about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Count

from urbanlens.dashboard.models.spotguessr.model import (
    GameSession,
    GameSessionParticipant,
    GameSessionStatus,
    PlayerModeRating,
    SpotGuessrPreference,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.spotguessr.session import GameConfig


def get_preference(profile: Profile) -> SpotGuessrPreference:
    """The profile's SpotGuessr preferences, creating the row on first access.

    Args:
        profile: The player.

    Returns:
        The existing or newly created preference row.
    """
    preference, _created = SpotGuessrPreference.objects.get_or_create(profile=profile)
    return preference


def remember_last_config(profile: Profile, config: GameConfig) -> SpotGuessrPreference:
    """Snapshot the settings a player just started a game with, as their new defaults.

    Called on every start, from every client, so returning to the game doesn't
    reset the difficulty slider and toggles every time. Stored as the config's
    plain dict rather than as individual columns because the set of settings
    changes with the game and a JSON snapshot doesn't need a migration to grow.

    Args:
        profile: The player who started a game.
        config: The settings they started it with.

    Returns:
        The saved preference row.
    """
    preference = get_preference(profile)
    preference.last_config = config.to_dict()
    preference.save(update_fields=["last_config", "updated"])
    return preference


def most_recent_rating(profile: Profile) -> PlayerModeRating | None:
    """The rating for whichever mode this player last played, or None if they never have.

    Deliberately *not* a fixed mode: ratings are per-mode, and a player who
    only plays Named Place has no Photos-mode row at all. Reading a hardcoded
    mode is why the overview page used to show "no rating" for such a player
    even though their rating was being updated correctly every round.

    Args:
        profile: The player.

    Returns:
        The most recently played ``PlayerModeRating``, or None.
    """
    return PlayerModeRating.objects.filter(profile=profile).order_by("-last_played_at").first()


def participated_sessions(profile: Profile, *, status: str | None = None) -> QuerySet[GameSession]:
    """Every session ``profile`` takes part in, annotated for list/detail display.

    Scoped through ``GameSessionParticipant`` rather than ``host_profile`` so a
    guest's own history includes games somebody else hosted. Two annotations
    ride along because both are wanted for every row and neither can be derived
    from the session itself without another query per row:

    - ``rounds_played``: how many rounds were ever created for the session.
    - ``participant_count``: the roster size, from which "is this multiplayer"
      follows. A caller must not answer that from ``status == LOBBY``: a
      multiplayer game that has already begun is ACTIVE, exactly like a solo
      one.

    The subquery (rather than a join to participants) keeps the two ``Count``
    annotations from multiplying each other's rows, which is the classic way an
    annotated count silently comes back inflated.

    Args:
        profile: The player whose sessions to list.
        status: Optional ``GameSessionStatus`` value to restrict to. An
            unrecognized value simply matches nothing, which is the honest
            answer for a filter naming a status that doesn't exist.

    Returns:
        An unevaluated queryset ordered newest-first.
    """
    session_ids = GameSessionParticipant.objects.filter(profile=profile).values("session_id")
    sessions = GameSession.objects.filter(pk__in=session_ids)
    if status is not None:
        sessions = sessions.filter(status=status)
    return sessions.annotate(
        rounds_played=Count("rounds", distinct=True),
        participant_count=Count("participants", distinct=True),
    ).order_by("-started_at")


def active_solo_session_id(profile: Profile) -> int | None:
    """The id of the profile's most recent still-playable *solo* session, if any.

    Solo specifically, because that is the only kind of session a resuming
    client can carry on with on its own: a multiplayer round's reveal is
    withheld until every joined participant has guessed and then arrives over
    the WebSocket, so pointing a client at one would leave it polling a round
    that never resolves. Multiplayer sessions still appear in the session
    *list* (flagged as such) - they just aren't offered as "resume this".

    Args:
        profile: The player.

    Returns:
        The session's primary key, or None when there is nothing to resume.
    """
    session = participated_sessions(profile, status=GameSessionStatus.ACTIVE).filter(participant_count=1).first()
    return session.pk if session is not None else None
