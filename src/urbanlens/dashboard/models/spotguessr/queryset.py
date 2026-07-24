"""QuerySets/Managers for SpotGuessr models.

Glicko-2 rating math lives in ``services.spotguessr.glicko2``; eligibility
and location/photo selection live in ``services.spotguessr.eligibility``/
``selection``/``photos``. These classes only scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.spotguessr.model import (
        GameRound,
        GameSession,
        GameSessionParticipant,
        Guess,
        LocationModeRating,
        PlayerModeRating,
    )


class PlayerModeRatingQuerySet(abstract.DashboardQuerySet["PlayerModeRating"]):
    """QuerySet for PlayerModeRating."""

    def for_profile(self, profile: Profile) -> PlayerModeRatingQuerySet:
        """Restrict to ``profile``'s own ratings, across all modes."""
        return self.filter(profile=profile)


class PlayerModeRatingManager(abstract.DashboardManager.from_queryset(PlayerModeRatingQuerySet)):
    """Manager for PlayerModeRating."""

    def get_or_create_for(self, profile: Profile, mode: str) -> PlayerModeRating:
        """Return ``profile``'s rating row for ``mode``, creating it at the default rating if missing."""
        rating, _ = self.get_or_create(profile=profile, mode=mode)
        return rating


class LocationModeRatingQuerySet(abstract.DashboardQuerySet["LocationModeRating"]):
    """QuerySet for LocationModeRating."""

    def for_location(self, location: Location) -> LocationModeRatingQuerySet:
        """Restrict to ``location``'s difficulty ratings, across all modes."""
        return self.filter(location=location)


class LocationModeRatingManager(abstract.DashboardManager.from_queryset(LocationModeRatingQuerySet)):
    """Manager for LocationModeRating."""

    def get_or_create_for(self, location: Location, mode: str) -> LocationModeRating:
        """Return ``location``'s difficulty rating row for ``mode``, creating it at the default rating if missing."""
        rating, _ = self.get_or_create(location=location, mode=mode)
        return rating


class GameSessionQuerySet(abstract.DashboardQuerySet["GameSession"]):
    """QuerySet for GameSession."""

    def active(self) -> GameSessionQuerySet:
        """Restrict to sessions still in progress."""
        from urbanlens.dashboard.models.spotguessr.model import GameSessionStatus

        return self.filter(status=GameSessionStatus.ACTIVE)

    def for_profile(self, profile: Profile) -> GameSessionQuerySet:
        """Restrict to sessions ``profile`` is (or was) a participant in."""
        return self.filter(participants__profile=profile).distinct()


class GameSessionManager(abstract.DashboardManager.from_queryset(GameSessionQuerySet)):
    """Manager for GameSession."""


class GameSessionParticipantQuerySet(abstract.DashboardQuerySet["GameSessionParticipant"]):
    """QuerySet for GameSessionParticipant."""


class GameSessionParticipantManager(abstract.DashboardManager.from_queryset(GameSessionParticipantQuerySet)):
    """Manager for GameSessionParticipant."""


class GameRoundQuerySet(abstract.DashboardQuerySet["GameRound"]):
    """QuerySet for GameRound."""

    def for_session(self, session: GameSession) -> GameRoundQuerySet:
        """Every round of ``session``, in play order."""
        return self.filter(session=session).order_by("sequence_index")


class GameRoundManager(abstract.DashboardManager.from_queryset(GameRoundQuerySet)):
    """Manager for GameRound."""


class GuessQuerySet(abstract.DashboardQuerySet["Guess"]):
    """QuerySet for Guess."""

    def for_round(self, round_: GameRound) -> GuessQuerySet:
        """Every guess submitted for ``round_``."""
        return self.filter(round=round_)


class GuessManager(abstract.DashboardManager.from_queryset(GuessQuerySet)):
    """Manager for Guess."""
