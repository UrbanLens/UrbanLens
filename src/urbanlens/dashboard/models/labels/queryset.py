"""QuerySet and Manager for Label."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class LabelQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for Label with visibility and ordering helpers."""

    def bulk_create(self, objs, *args, **kwargs):
        """Create labels in bulk, coercing each colour first.

        ``bulk_create`` does not call ``save()``, so the model's coercion has to
        be repeated here or a bulk path stores what a single write would reject.

        Args:
            objs: The labels to create.
            *args: Passed through to Django's ``bulk_create``.
            **kwargs: Passed through to Django's ``bulk_create``.

        Returns:
            The created labels, as Django's ``bulk_create`` returns them.
        """
        objs = list(objs)
        for obj in objs:
            obj.coerce_colors()
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        """Update labels in bulk, coercing each colour first.

        The third path past ``save()``, and the one the external API's bulk edit
        uses. That endpoint validates its input and 400s on a bad colour, so
        this is the backstop for every other caller.

        Args:
            objs: The labels to update.
            fields: The column names to write.
            *args: Passed through to Django's ``bulk_update``.
            **kwargs: Passed through to Django's ``bulk_update``.

        Returns:
            Whatever Django's ``bulk_update`` returns - the number of rows
            matched, on the versions that report it.
        """
        objs = list(objs)
        if "color" in fields:
            for obj in objs:
                obj.coerce_colors()
        return super().bulk_update(objs, fields, *args, **kwargs)

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
        return self.filter(kind=KIND_TAG)

    def categories(self) -> Self:
        """Return only items with kind='category'."""
        return self.filter(kind=KIND_CATEGORY)

    def statuses(self) -> Self:
        """Return only items with kind='status'."""
        return self.filter(kind=KIND_STATUS)

    def user_labels(self) -> Self:
        """Return only items with kind='user' (for annotating profiles privately)."""
        return self.filter(kind=KIND_USER)

    def media(self) -> Self:
        """Return only items with kind='media' (attached to photos/videos/documents, not pins)."""
        # Don't hardcode strings
        return self.filter(kind=KIND_MEDIA)

    def suggestable(self) -> Self:
        """Return only tag/category labels - the sole kinds ever synced to REData's label-suggestion service.

        Status, people, and media labels are never sent (per the explicit
        product decision - see ``services.labels.redata_suggestions``).
        """
        return self.filter(kind__in=(KIND_TAG, KIND_CATEGORY))

    def location_labels(self) -> Self:
        """Return only items assignable to pins/wikis (excludes 'user' and 'media', which attach elsewhere)."""
        return self.exclude(kind__in=(KIND_USER, KIND_MEDIA))

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

    def with_hierarchy(self) -> Self:
        """Prefetch parents/children without computing pin or location counts.

        Cheap counterpart to `with_pin_counts()` for a page's first paint: the
        Organize page renders label cards from this immediately, then a
        follow-up HTMX request re-fetches the same rows via `with_pin_counts()`
        to back-fill the stat badges once they're ready, so the DOM shows up
        before the count queries (including the per-label descendant BFS in
        `tag_total_pins`) have run at all.
        """
        from urbanlens.dashboard.models.labels.model import Label

        return self.prefetch_related(
            Prefetch("children", queryset=Label.objects.only("id", "name", "kind")),
            Prefetch("parents", queryset=Label.objects.only("id", "name", "kind")),
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

    def in_display_order(self) -> Self:
        """Rank order, then name.

        Not called `ordered`: Django's `QuerySet.ordered` is a bool property,
        and a method of the same name shadows it, so anything reading it as a
        bool - `Paginator` does, via `getattr(object_list, "ordered", None)` -
        sees a truthy bound method instead of the property's answer. Nothing
        misbehaved in practice, because `Label.Meta.ordering` is set and that
        property would have returned True anyway; the rename is to stop a
        subclass silently redefining a documented part of the QuerySet API,
        which is what mypy's `override` check flagged.
        """
        return self.order_by("-order", "name")


class LabelManager(abstract.FrontendDashboardManager.from_queryset(LabelQuerySet)):
    pass
