# Generic imports
from __future__ import annotations

from typing import TYPE_CHECKING, Self

# Django Imports
from django.db.models import Q

# App Imports
from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.achievements.model import Achievement, ProfileActivityDay, ProfileStreak, UserAchievement
    from urbanlens.dashboard.models.profile import Profile


class AchievementQuerySet(abstract.PublicDashboardQuerySet["Achievement"]):
    """Filters for the admin-defined award catalogue."""

    def active(self) -> Self:
        """Return only awards that are still being granted and listed."""
        return self.filter(is_active=True)

    def for_metric(self, metric: str | list[str]) -> Self:
        """Return awards measuring one metric, or any of several.

        Args:
            metric: A single registry key, or a list of them.
        """
        if isinstance(metric, str):
            return self.filter(metric=metric)
        return self.filter(metric__in=metric)

    def listable_for(self, profile: Profile | int | None) -> Self:
        """Return awards a given viewer should see in a catalogue listing.

        Secret awards stay hidden until earned, so they are included only when
        *profile* already holds them.

        Args:
            profile: The profile whose earned set unlocks secret awards, or
                None for an anonymous/unknown viewer.
        """
        if profile is None:
            return self.filter(is_secret=False)
        profile_id = profile if isinstance(profile, int) else profile.pk
        return self.filter(Q(is_secret=False) | Q(awards__profile_id=profile_id)).distinct()


class AchievementManager(abstract.PublicDashboardManager.from_queryset(AchievementQuerySet)):
    pass


class UserAchievementQuerySet(abstract.FrontendDashboardQuerySet["UserAchievement"]):
    """Filters for awards that have actually been earned."""

    def for_profile(self, profile: Profile | int) -> Self:
        """Return every award earned by one profile."""
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def displayable(self) -> Self:
        """Return earned awards ready to render - newest first, award prefetched.

        Retired (``is_active=False``) awards are kept: revoking the display of
        something a user already earned would read as the award being taken
        away.
        """
        return self.select_related("achievement").order_by("-earned_at")

    def earned_achievement_ids(self) -> set[int]:
        """Return the set of ``Achievement`` PKs represented in this queryset."""
        return set(self.values_list("achievement_id", flat=True))


class UserAchievementManager(abstract.FrontendDashboardManager.from_queryset(UserAchievementQuerySet)):
    pass


class ProfileActivityDayQuerySet(abstract.DashboardQuerySet["ProfileActivityDay"]):
    """Filters over the raw per-day activity log that backs streaks."""

    def for_profile(self, profile: Profile | int) -> Self:
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def of_kind(self, kind: str) -> Self:
        return self.filter(kind=kind)

    def since(self, day: datetime.date) -> Self:
        return self.filter(day__gte=day)


class ProfileActivityDayManager(abstract.DashboardManager.from_queryset(ProfileActivityDayQuerySet)):
    pass


class ProfileStreakQuerySet(abstract.DashboardQuerySet["ProfileStreak"]):
    """Filters over cached streak lengths."""

    def for_profile(self, profile: Profile | int) -> Self:
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def of_kind(self, kind: str) -> Self:
        return self.filter(kind=kind)


class ProfileStreakManager(abstract.DashboardManager.from_queryset(ProfileStreakQuerySet)):
    pass
