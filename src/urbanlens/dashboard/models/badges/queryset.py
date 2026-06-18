"""QuerySet and Manager for Badge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Count, Prefetch, Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class BadgeQuerySet(abstract.QuerySet):
    """QuerySet for Badge with visibility and ordering helpers."""

    def visible_to(self, profile: Profile | int) -> Self:
        """Return global badges (profile=None) plus badges owned by this profile."""
        if isinstance(profile, int):
            return self.filter(Q(profile__isnull=True) | Q(profile_id=profile))
        return self.filter(Q(profile__isnull=True) | Q(profile=profile))

    def global_only(self) -> Self:
        """Return only global badges (profile=None)."""
        return self.filter(profile__isnull=True)

    def for_profile(self, profile: Profile | int) -> Self:
        """Return badges owned by a specific profile (not global)."""
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def with_icon(self) -> Self:
        """Badges that have at least one icon set (standard or custom)."""
        return self.filter(Q(custom_icon__gt="") | Q(icon__gt=""))

    def tags(self) -> Self:
        """Return only items with kind='tag'."""
        return self.filter(kind="tag")

    def categories(self) -> Self:
        """Return only items with kind='category'."""
        return self.filter(kind="category")

    def statuses(self) -> Self:
        """Return only items with kind='status'."""
        return self.filter(kind="status")

    def with_customizations_for(self, profile: Profile | int) -> Self:
        """Prefetch this user's BadgeCustomizations into _user_customizations attr."""
        from urbanlens.dashboard.models.badges.customization import BadgeCustomization

        profile_id = profile if isinstance(profile, int) else profile.pk
        return self.prefetch_related(
            Prefetch(
                "customizations",
                queryset=BadgeCustomization.objects.filter(profile_id=profile_id),
                to_attr="_user_customizations",
            ),
        )

    def with_pin_counts(self) -> Self:
        """Annotate pin_count / location_count and prefetch children (with their own pin_count) and parents.

        Replaces heavy prefetch_related('pins', 'categorized_pins', ...) with lightweight COUNT annotations.
        pin_count covers both tag pins (related_name="pins") and status pins (related_name="status_pins").
        """
        return self.annotate(
            pin_count=Count("pins", distinct=True) + Count("status_pins", distinct=True),
            location_count=Count("categorized_locations", distinct=True),
        ).prefetch_related(
            Prefetch(
                "children",
                queryset=self.model.objects.annotate(pin_count=Count("pins", distinct=True)),
            ),
            Prefetch("parents", queryset=self.model.objects.only("id", "name")),
        )

    def ordered(self) -> Self:
        return self.order_by("-order", "name")


class BadgeManager(abstract.Manager.from_queryset(BadgeQuerySet)):
    pass
