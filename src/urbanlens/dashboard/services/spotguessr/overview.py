"""What a client needs to render "the SpotGuessr screen you land on"."""

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
    Called on every start, from every client, so returning to the game doesn't reset the difficulty slider and toggles every time.

    Args:
        profile: The player who started a game.
        config: The settings they started it with.

    Returns:
        The saved preference row."""
    preference = get_preference(profile)
    preference.last_config = config.to_dict()
    preference.save(update_fields=["last_config", "updated"])
    return preference


def most_recent_rating(profile: Profile) -> PlayerModeRating | None:
    """The rating for whichever mode this player last played, or None if they never have.

    Args:
        profile: The player.

    Returns:
        The most recently played ``PlayerModeRating``, or None."""
    return PlayerModeRating.objects.filter(profile=profile).order_by("-last_played_at").first()


def participated_sessions(profile: Profile, *, status: str | None = None) -> QuerySet[GameSession]:
    """Every session ``profile`` takes part in, annotated for list/detail display.

    Args:
        profile: The player whose sessions to list.
        status: Optional ``GameSessionStatus`` value to restrict to.

    Returns:
        An unevaluated queryset ordered newest-first."""
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

    Args:
        profile: The player.

    Returns:
        The session's primary key, or None when there is nothing to resume."""
    session = participated_sessions(profile, status=GameSessionStatus.ACTIVE).filter(participant_count=1).first()
    return session.pk if session is not None else None
