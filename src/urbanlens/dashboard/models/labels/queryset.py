"""QuerySet and Manager for Label."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class LabelQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for Label with visibility and ordering helpers."""

    def visible_to(self, profile: Profile | int) -> Self:
        """Return global labels (profile=None) plus labels owned by this profile."""
        if isinstance(profile, int):
            return self.filter(Q(profile__isnull=True) | Q(profile_id=profile))
        return self.filter(Q(profile__isnull=True) | Q(profile=profile))

    def global_only(self) -> Self:
        """Return only global labels (profile=None)."""
        return self.filter(profile__isnull=True)

    def for_profile(self, profile: Profile | int) -> Self:
        """Return labels owned by a specific profile (not global)."""
        if isinstance(profile, int):
            return self.filter(profile_id=profile)
        return self.filter(profile=profile)

    def with_icon(self) -> Self:
        """Labels that have at least one icon set (standard or custom)."""
        return self.filter(Q(custom_icon__gt="") | Q(icon__gt=""))

    def tags(self) -> Self:
        """Return only items with kind='tag'."""
        # Don't hardcode strings
        return self.filter(kind="tag")

    def categories(self) -> Self:
        """Return only items with kind='category'."""
        # Don't hardcode strings
        return self.filter(kind="category")

    def statuses(self) -> Self:
        """Return only items with kind='status'."""
        # Don't hardcode strings
        return self.filter(kind="status")

    def user_labels(self) -> Self:
        """Return only items with kind='user' (for annotating profiles privately)."""
        # TODO: Don't hardcode 'user' string
        return self.filter(kind="user")

    def media(self) -> Self:
        """Return only items with kind='media' (attached to photos/videos/documents, not pins)."""
        # Don't hardcode strings
        return self.filter(kind="media")

    def location_labels(self) -> Self:
        """Return only items assignable to pins/wikis (excludes 'user' and 'media', which attach elsewhere)."""
        # TODO: Don't hardcode 'user' string
        return self.exclude(kind__in=["user", "media"])

    def with_customizations_for(self, profile: Profile | int) -> Self:
        """Prefetch this user's LabelCustomizations into _user_customizations attr."""
        from urbanlens.dashboard.models.labels.customization import LabelCustomization

        profile_id = profile if isinstance(profile, int) else profile.pk
        return self.prefetch_related(
            Prefetch(
                "customizations",
                queryset=LabelCustomization.objects.filter(profile_id=profile_id),
                to_attr="_user_customizations",
            ),
        )

    def with_pin_counts(self) -> Self:
        """Annotate pin_count / location_count and prefetch children (with their own pin_count) and parents.

        Each count is a correlated subquery rather than a sibling `Count()` on the
        same queryset - annotating `pins` and `wikis` together would join both M2M
        tables in before grouping, producing a row per (pin, wiki) pair per label
        (a cartesian fan-out) that `distinct=True` only fixes after the fact.
        """
        from urbanlens.dashboard.models.labels.model import Label

        pin_counts = Label.objects.filter(pk=OuterRef("pk")).order_by().values("pk").annotate(c=Count("pins")).values("c")
        wiki_counts = Label.objects.filter(pk=OuterRef("pk")).order_by().values("pk").annotate(c=Count("wikis")).values("c")

        return self.annotate(
            pin_count=Coalesce(Subquery(pin_counts, output_field=IntegerField()), 0),
            location_count=Coalesce(Subquery(wiki_counts, output_field=IntegerField()), 0),
        ).prefetch_related(
            Prefetch(
                "children",
                queryset=Label.objects.annotate(pin_count=Count("pins", distinct=True)),
            ),
            Prefetch("parents", queryset=Label.objects.only("id", "name", "kind")),
        )

    def ordered(self) -> Self:
        return self.order_by("-order", "name")


class LabelManager(abstract.FrontendDashboardManager.from_queryset(LabelQuerySet)):
    pass
