"""QuerySet and Manager for Label."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from django.db import IntegrityError, transaction
from django.db.models import Count, F, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER, PROFILE_SCOPED_KINDS

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile


class LabelNameConflictError(Exception):
    """A label of that name and kind is already visible to the profile.

    Attributes:
        conflict: The existing label, the profile's own before a global one.
    """

    def __init__(self, conflict: Label) -> None:
        super().__init__(f"A {conflict.kind} named {conflict.name!r} already exists.")
        self.conflict = conflict


class LabelQuerySet(abstract.FrontendDashboardQuerySet["Label"]):
    """QuerySet for Label with visibility and ordering helpers."""

    def bulk_create(self, objs, *args, **kwargs):
        """Create labels in bulk, coercing each colour first.
        ``bulk_create`` does not call ``save()``, so the model's coercion has to be repeated here or a bulk path stores what a single write would reject.

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
            obj.coerce_icon()
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        """Update labels in bulk, coercing each colour first.
        The third path past ``save()``, and the one the external API's bulk edit uses.
        That endpoint validates its input and 400s on a bad colour, so this is the backstop for every other caller.

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
        for obj in objs:
            if "color" in fields:
                obj.coerce_colors()
            if "icon" in fields:
                obj.coerce_icon()
        return super().bulk_update(objs, fields, *args, **kwargs)

    def visible_to(self, profile: Profile | int) -> Self:
        """Return global labels (profile=None) plus labels owned by this profile."""
        if isinstance(profile, int):
            return self.filter(Q(profile__isnull=True) | Q(profile_id=profile))
        return self.filter(Q(profile__isnull=True) | Q(profile=profile))

    def named(self, profile: Profile | int, name: str, kind: str) -> Self:
        """Labels of *kind* visible to *profile* whose name matches *name* case-insensitively, own before global.

        The lookup every create-by-name must use: it matches the ``(lower(name), profile, kind)`` constraint, and it
        also sees a global label a personal one would shadow. A profile-scoped kind (category, status) only ever
        matches the profile's own labels.

        Args:
            profile: The profile whose own and global labels are searched.
            name: The name to match; surrounding whitespace is ignored.
            kind: The label kind.

        Returns:
            The matches, the profile's own first.
        """
        scope = self.for_profile(profile) if kind in PROFILE_SCOPED_KINDS else self.visible_to(profile)
        return scope.filter(name__iexact=name.strip(), kind=kind).order_by(F("profile").asc(nulls_last=True))

    def resolve_or_create(self, profile: Profile, name: str, kind: str, *, defaults: dict[str, Any] | None = None) -> tuple[Label, bool]:
        """Return the label *profile* sees under *name*, creating a personal one when there is none.

        Args:
            profile: The owner of a created label, and whose own labels win over global ones.
            name: The label name; stripped, and matched case-insensitively.
            kind: The label kind.
            defaults: Field values applied only to a created label.

        Returns:
            ``(label, created)``.

        Raises:
            ValueError: *name* is blank.
            IntegrityError: The insert failed for a reason other than a concurrent insert of the same name.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("A label name cannot be blank.")
        if (existing := self.named(profile, cleaned, kind).first()) is not None:
            return existing, False
        try:
            with transaction.atomic():
                return self.create(profile=profile, name=cleaned, kind=kind, **(defaults or {})), True
        except IntegrityError:
            if (existing := self.named(profile, cleaned, kind).first()) is None:
                raise
            return existing, False

    def create_unique(self, *, profile: Profile, name: str, kind: str, **fields: Any) -> Label:
        """Create a personal label, refusing a name the profile already sees.

        For write paths that report a collision instead of reusing the existing label. A concurrent insert of the
        same name is reported the same way as one that was already there.

        Args:
            profile: The owner.
            name: The label name; stripped.
            kind: The label kind.
            **fields: Any other field values.

        Returns:
            The created label.

        Raises:
            LabelNameConflictError: A label of that name and kind is already visible to *profile*.
            IntegrityError: The insert failed for another reason.
        """
        cleaned = name.strip()
        if (existing := self.named(profile, cleaned, kind).first()) is not None:
            raise LabelNameConflictError(existing)
        try:
            with transaction.atomic():
                return self.create(profile=profile, name=cleaned, kind=kind, **fields)
        except IntegrityError:
            if (existing := self.named(profile, cleaned, kind).first()) is None:
                raise
            raise LabelNameConflictError(existing) from None

    def pin_assignable_by(self, profile: Profile | int) -> Self:
        """Return the labels *profile* may put on a pin: its own and global ones, of a pin-assignable kind."""
        return self.visible_to(profile).location_labels()

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
        For callers that render no stats at all. Deferring the stats of a page that does render them is not worth it: measured at 120 labels, the counts cost ~16ms against ~100ms to render the cards they sit in (X25).
        """
        from urbanlens.dashboard.models.labels.model import Label

        return self.prefetch_related(
            Prefetch("children", queryset=Label.objects.only("id", "name", "kind")),
            Prefetch("parents", queryset=Label.objects.only("id", "name", "kind")),
        )

    def with_pin_counts(self) -> Self:
        """Annotate pin_count / location_count and prefetch children (with their own pin_count) and parents.
        Each count is a correlated subquery rather than a sibling `Count()` on the same queryset - annotating `pins` and `wikis` together would join both M2M tables in before grouping, producing a row per (pin, wiki) pair per label (a cartesian fan-out) that `distinct=True` only fixes after the fact.
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
        Not called `ordered`: Django's `QuerySet.ordered` is a bool property, and a method of the same name shadows it, so anything reading it as a bool - `Paginator` does, via `getattr(object_list, "ordered", None)` - sees a truthy bound method instead of the property's answer.
        """
        return self.order_by("-order", "name")


class LabelManager(abstract.FrontendDashboardManager.from_queryset(LabelQuerySet)):
    pass
