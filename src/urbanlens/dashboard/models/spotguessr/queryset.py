"""QuerySets/Managers for SpotGuessr models.

Glicko-2 rating math lives in ``services.spotguessr.glicko2``; eligibility
and location/photo selection live in ``services.spotguessr.eligibility``/
``selection``/``photos``. These classes only scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.spotguessr.model import (
        GameRound,
        GameSession,
        GameSessionChatMessage,
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
        """Restrict to sessions still in progress (lobby or active)."""
        from urbanlens.dashboard.models.spotguessr.model import GameSessionStatus

        return self.filter(status__in=[GameSessionStatus.LOBBY, GameSessionStatus.ACTIVE])

    def for_profile(self, profile: Profile) -> GameSessionQuerySet:
        """Restrict to sessions ``profile`` is (or was) a participant in, any status."""
        return self.filter(participants__profile=profile).distinct()

    def stalled(self, *, cutoff: datetime) -> GameSessionQuerySet:
        """ACTIVE sessions whose current round was created before ``cutoff`` and still isn't revealed.

        ``get_or_create_round`` never creates a session's next round until
        its prior one is fully revealed, so at most one round per session
        can ever match "unrevealed" at a time - this is always that
        session's current round. Used by the stall-sweep Celery task
        (``tasks.sweep_stalled_spotguessr_sessions``) to find sessions a
        participant walked away from mid-round (see
        ``services.spotguessr.session.force_reveal_round``).
        """
        from urbanlens.dashboard.models.spotguessr.model import GameSessionStatus

        return self.filter(status=GameSessionStatus.ACTIVE, rounds__revealed_at__isnull=True, rounds__created__lte=cutoff).distinct()


class GameSessionManager(abstract.DashboardManager.from_queryset(GameSessionQuerySet)):
    """Manager for GameSession."""


class GameSessionParticipantQuerySet(abstract.DashboardQuerySet["GameSessionParticipant"]):
    """QuerySet for GameSessionParticipant."""

    def joined(self) -> GameSessionParticipantQuerySet:
        """Restrict to participants who have actually accepted (not just invited)."""
        from urbanlens.dashboard.models.spotguessr.model import GameSessionParticipantStatus

        return self.filter(status=GameSessionParticipantStatus.JOINED)


class GameSessionParticipantManager(abstract.DashboardManager.from_queryset(GameSessionParticipantQuerySet)):
    """Manager for GameSessionParticipant."""


class GameSessionChatMessageQuerySet(abstract.DashboardQuerySet["GameSessionChatMessage"]):
    """QuerySet for GameSessionChatMessage."""

    def for_session(self, session: GameSession) -> GameSessionChatMessageQuerySet:
        """Every chat message in ``session``, oldest first."""
        return self.filter(session=session).order_by("created")


class GameSessionChatMessageManager(abstract.DashboardManager.from_queryset(GameSessionChatMessageQuerySet)):
    """Manager for GameSessionChatMessage."""


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
