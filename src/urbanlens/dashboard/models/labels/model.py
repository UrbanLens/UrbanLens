"""Label model - a named label applied to pins, with optional user ownership and hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    Count,
    ForeignKey,
    ImageField,
    Index,
    IntegerField,
    ManyToManyField,
    Min,
    Q,
    TextField,
    UniqueConstraint,
    UUIDField,
)
from django.db.models.functions import Lower

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, KIND_CATEGORY, KIND_CHOICES, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.queryset import LabelManager

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.labels.customization import LabelCustomization
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


class Label(abstract.FrontendDashboardModel):
    """A named label that can be applied to pins.

    Labels are either global (profile=None, visible to all users) or user-specific
    (profile set, only visible to that user and alongside global labels).

    Labels form an arbitrary-depth hierarchy via the parents M2M. Filtering by a label
    also matches any descendant labels (use get_label_and_descendants for the full set).

    The `kind` field distinguishes between tag-type labels (personal labels) and
    category-type labels (global shared classification). Labels absorb the functionality
    of the former PinList model: they carry an icon, custom icon, color, description,
    and ordering weight that feeds into Pin.effective_icon's priority chain.
    """

    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    # Hex color string chosen from COLOR_CHOICES (e.g. "#2196F3").
    color = CharField(max_length=50, null=True, blank=True, choices=COLOR_CHOICES)
    icon = CharField(max_length=50, null=True, blank=True)  # emoji char or Material Icons name
    custom_icon = ImageField(upload_to="label_icons/", null=True, blank=True)
    # Discriminates tags from categories (and any future kinds).
    kind = CharField(max_length=20, choices=KIND_CHOICES, default=KIND_TAG, db_index=True)
    # Higher order = checked first in the icon priority chain.
    order = IntegerField(default=0)
    # Protected labels (e.g. the built-in "Visited" status) cannot be deleted or renamed.
    is_protected = BooleanField(default=False)
    # When False, auto-tagging (keyword or AI) will never attach this label to a pin.
    allow_auto_tag = BooleanField(default=True)
    # Comma-separated keywords/phrases used by the keyword auto-tagger in addition to the label name.
    keywords = TextField(null=True, blank=True)

    # NULL = global tag visible to all users; non-null = owned by one user.
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_labels",
    )

    # Hierarchical parents - symmetrical=False so parent→child is one direction.
    parents: ManyToManyField[Label, Label] = ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="children",
    )

    if TYPE_CHECKING:
        profile_id: int | None
        pins: ManyToManyField[Pin, Pin]
        wikis: ManyToManyField[Wiki, Wiki]
        # Transient bookkeeping for the pre/post-save REData-taxonomy kind-change hook.
        redata_prior_kind: str | None

    objects = LabelManager()

    # Per-instance memo for total_pin_count(); not a field.
    _total_pins_memo: int | None = None

    def _get_customization(self) -> LabelCustomization | None:
        """Return this user's customization, if the queryset was prefetched."""
        cached: list[LabelCustomization] = getattr(self, "_user_customizations", [])
        return cached[0] if cached else None

    @property
    def effective_name(self) -> str:
        """Property that returns the user's override name, or falls back to the global name."""
        c = self._get_customization()
        return (c.name if c and c.name else None) or self.name

    @property
    def effective_icon(self) -> str | None:
        """Property that returns the user's override icon, or falls back to the global icon."""
        c = self._get_customization()
        if c and c.icon is not None:
            return c.icon
        return self.icon

    @property
    def effective_color(self) -> str | None:
        """Property that returns the user's override color, or falls back to the global color."""
        c = self._get_customization()
        if c and c.color is not None:
            return c.color
        return self.color

    @property
    def is_customized(self) -> bool:
        """True if this user has any active override for this tag."""
        c = self._get_customization()
        return c is not None and any([c.name, c.icon is not None, c.color is not None])

    @property
    def icon_is_overridden(self) -> bool:
        """True if this user has explicitly set an icon override (bypasses custom_icon)."""
        c = self._get_customization()
        return c is not None and c.icon is not None

    @classmethod
    def prime_total_pin_counts(cls, labels: Sequence[Label]) -> None:
        """Precompute :meth:`total_pin_count` for a whole page of labels at once.

        ``total_pin_count`` is correct but per-instance: each call runs its own
        BFS - which issues one query *per node visited* - plus a `Count`
        aggregate, and memoizes only on that instance. Rendering N labels
        therefore costs O(N x subtree) queries. Measured on the Organize page's
        deferred rows endpoint: 143 labels cost 113-146 queries, growing exactly
        one-per-label.

        This resolves the same numbers in a fixed three queries by loading the
        edge list once and doing the traversal in Python, then seeding each
        instance's memo so the template filter and every later call read it
        without touching the database. The edge list is scoped to labels
        owned by *labels*' own profile(s), plus global labels, rather than
        the whole site's - nothing lets one profile's label parent/child
        another's, so a rendered label's subtree can never reach an edge
        outside that set, and this never has to load every other profile's
        unrelated hierarchy to answer it.

        Safe to skip: any label not primed still computes itself on demand, so
        callers that render a single label need not change.

        Args:
            labels: The label instances about to be rendered. Must be the same
                objects the template will use - priming a queryset that is
                re-evaluated later seeds memos on discarded instances.
        """
        labels = list(labels)
        if not labels:
            return

        # One query for the edge list, scoped to the profile(s) that own the
        # rendered labels (plus global labels) instead of every profile's
        # private hierarchy site-wide. The subtree of a rendered label can
        # reach labels outside the rendered set (a tag's child that this
        # kind's filter excluded), so this still spans every label owned by
        # the relevant profile(s), not only the ones on screen.
        owning_profile_ids = {label.profile_id for label in labels if label.profile_id is not None}
        visible_edges = Q(from_label__profile_id__isnull=True) | Q(to_label__profile_id__isnull=True)
        if owning_profile_ids:
            visible_edges |= Q(from_label__profile_id__in=owning_profile_ids) | Q(to_label__profile_id__in=owning_profile_ids)
        children_by_parent: dict[int, list[int]] = {}
        for child_id, parent_id in cls.parents.through.objects.filter(visible_edges).values_list("from_label_id", "to_label_id"):
            children_by_parent.setdefault(parent_id, []).append(child_id)

        def descendants(root: int) -> set[int]:
            """Every id beneath *root*, cycle-safe, matching get_label_and_descendants."""
            seen: set[int] = set()
            queue = [root]
            while queue:
                current = queue.pop()
                if current in seen:
                    continue
                seen.add(current)
                queue.extend(children_by_parent.get(current, ()))
            return seen

        needed: set[int] = set()
        subtrees: dict[int, set[int]] = {}
        for label in labels:
            if label.pk is None:
                continue
            subtree = descendants(label.pk)
            subtrees[label.pk] = subtree
            needed |= subtree

        # One query for every pin count involved, annotated rather than counted
        # per label.
        counts = dict(cls.objects.filter(pk__in=needed).annotate(n=Count("pins")).values_list("pk", "n"))

        for label in labels:
            if label.pk is None:
                continue
            label._total_pins_memo = sum(counts.get(pk, 0) for pk in subtrees[label.pk])  # noqa: SLF001 - seeding this class's own memo on its own instances

    def total_pin_count(self) -> int:
        """Return this label's pin count plus every descendant's pin count (full subtree).

        Walks the full multi-level hierarchy via ``get_label_and_descendants``
        (BFS, cycle-safe) rather than only direct children, matching how map/pin
        filtering actually expands a parent label to its whole subtree.

        Uses the annotated ``pin_count`` for this label when the queryset
        supplied one (``LabelQuerySet.with_pin_counts()``); falls back to a DB
        query otherwise. Descendant counts beyond the prefetched direct children
        are always summed via a single aggregate query, since only the
        direct-children prefetch carries its own annotation.

        The result is memoized on the instance: an Organize label card reads it
        up to three times (the compact badge, the stats column, and the "View on
        map" button's empty check), and the BFS plus aggregate behind it is the
        expensive part of that page.

        Returns:
            Total pins carried by this label or any label beneath it.
        """
        if self._total_pins_memo is not None:
            return self._total_pins_memo

        annotated: int | None = getattr(self, "pin_count", None)
        total: int = annotated if annotated is not None else self.pins.count()

        if self.pk is not None:
            descendant_ids = self.get_label_and_descendants(self.pk) - {self.pk}
            if descendant_ids:
                total += Label.objects.filter(id__in=descendant_ids).aggregate(total=Count("pins"))["total"] or 0

        self._total_pins_memo = total
        return total

    @classmethod
    def initial_order_for_parents(
        cls,
        profile: Profile,
        parent_ids: list[str] | list[int],
    ) -> int | None:
        """Return ``order`` for a new label placed just above its highest-priority parent.

        When parents are chosen at creation time, the new label is placed immediately
        above the highest-priority parent among them. When multiple parents are
        selected, the parent with the smallest ``order`` value is used (e.g.
        Hospital at order 20 rather than Pennsylvania at order 35). The new label
        receives that parent's ``order`` minus one (20 → 19).

        Args:
            profile: Owner profile used to resolve visible parent labels.
            parent_ids: Primary keys of selected parent labels.

        Returns:
            Computed order, or ``None`` when ``parent_ids`` is empty or no valid
            parents are found (callers should keep the default creation order).
        """
        if not parent_ids:
            return None
        result = cls.objects.visible_to(profile).filter(id__in=parent_ids).aggregate(reference_order=Min("order"))
        reference_order = result["reference_order"]
        if reference_order is None:
            return None
        return reference_order - 1

    @classmethod
    def get_label_and_descendants(cls, label_id: int) -> set[int]:
        """Return label_id plus all descendant label IDs (BFS, cycle-safe).

        Used so that filtering pins by a parent label also surfaces pins carrying
        any of its descendant labels.
        """
        visited: set[int] = set()
        queue: list[int] = [label_id]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            children_ids = list(cls.objects.filter(parents__id=current).values_list("id", flat=True))
            queue.extend(children_ids)
        return visited

    @property
    def is_global(self) -> bool:
        """Whether this is a site-wide label rather than one a user owns.

        Reads ``profile_id`` rather than ``profile`` so templates can ask this
        per row without fetching the owning profile: ``{% if not label.profile %}``
        issued one query per label per occurrence, and the Organize page asks it
        several times for each card.

        Returns:
            True when no profile owns this label.
        """
        return self.profile_id is None

    def __str__(self) -> str:
        if self.profile_id:
            return f"{self.name} ({self.profile})"
        return f"{self.name} [global]"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_labels"
        ordering = ["-order", "name"]
        get_latest_by = "updated"
        permissions = [("edit_global_label", "Can edit global labels")]
        indexes = [
            Index(fields=["profile", "order"], name="idxdb_label_pfile_ord"),
        ]
        constraints = [
            # Case-insensitive, matching how PinAlias/WikiAlias already model the
            # same "name identifies a row within its parent" relationship, and
            # matching what callers assume: several sites treat
            # (profile, name, kind) as identifying, and media_labels.py had to
            # pre-filter with ``name__iexact`` because ``get_or_create(name=...)``
            # alone is case-sensitive while the intended identity is not.
            #
            # ``nulls_distinct=False`` so global labels (profile IS NULL) are
            # constrained against each other too - Postgres treats NULLs as
            # distinct by default, which would leave duplicate globals possible.
            # Requires Postgres 15+; this project runs 17.
            UniqueConstraint(
                Lower("name"),
                "profile",
                "kind",
                name="uq_label_profile_name_kind_ci",
                nulls_distinct=False,
            ),
        ]
