"""QuerySet and Manager for SocialLink."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class SocialLinkQuerySet(abstract.DashboardQuerySet):
    def for_profile(self, profile: Profile | int) -> Self:
        """Return all links belonging to a given profile."""
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def platform(self, platform: str) -> Self:
        """Filter to a specific platform key."""
        return self.filter(platform=platform)


class SocialLinkManager(abstract.DashboardManager.from_queryset(SocialLinkQuerySet)):
    pass
