"""QuerySet and Manager for Tag."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class TagQuerySet(abstract.QuerySet):
    """QuerySet for Tag with visibility and ordering helpers."""

    def visible_to(self, profile: Profile | int) -> Self:
        """Return global tags (profile=None) plus tags owned by this profile."""
        if isinstance(profile, int):
            return self.filter(Q(profile__isnull=True) | Q(profile_id=profile))
        return self.filter(Q(profile__isnull=True) | Q(profile=profile))

    def global_only(self) -> Self:
        """Return only global tags (profile=None)."""
        return self.filter(profile__isnull=True)

    def for_profile(self, profile: Profile | int) -> Self:
        """Return tags owned by a specific profile (not global)."""
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def with_icon(self) -> Self:
        """Tags that have at least one icon set (standard or custom)."""
        return self.filter(Q(custom_icon__gt="") | Q(icon__gt=""))

    def ordered(self) -> Self:
        return self.order_by("-order", "name")


class TagManager(abstract.Manager.from_queryset(TagQuerySet)):
    pass
