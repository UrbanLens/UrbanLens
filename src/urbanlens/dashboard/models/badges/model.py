"""Badge model - a named label applied to pins, with optional user ownership and hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    ForeignKey,
    ImageField,
    Index,
    IntegerField,
    ManyToManyField,
    Min,
    TextField,
    UUIDField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.badges.meta import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, KIND_CATEGORY, KIND_CHOICES, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.badges.queryset import BadgeManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.customization import BadgeCustomization
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


class Badge(abstract.Model):
    """A named label that can be applied to pins.

    Badges are either global (profile=None, visible to all users) or user-specific
    (profile set, only visible to that user and alongside global badges).

    Badges form an arbitrary-depth hierarchy via the parents M2M. Filtering by a badge
    also matches any descendant badges (use get_badge_and_descendants for the full set).

    The `kind` field distinguishes between tag-type badges (personal labels) and
    category-type badges (global shared classification). Badges absorb the functionality
    of the former PinList model: they carry an icon, custom icon, color, description,
    and ordering weight that feeds into Pin.effective_icon's priority chain.
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    # Hex color string chosen from COLOR_CHOICES (e.g. "#2196F3").
    color = CharField(max_length=50, null=True, blank=True, choices=COLOR_CHOICES)
    icon = CharField(max_length=50, null=True, blank=True)  # emoji char or Material Icons name
    custom_icon = ImageField(upload_to="tag_icons/", null=True, blank=True)
    # Discriminates tags from categories (and any future kinds).
    kind = CharField(max_length=20, choices=KIND_CHOICES, default=KIND_TAG, db_index=True)
    # Higher order = checked first in the icon priority chain.
    order = IntegerField(default=0)
    # Protected badges (e.g. the built-in "Visited" status) cannot be deleted or renamed.
    is_protected = BooleanField(default=False)
    # When False, auto-tagging (keyword or AI) will never attach this badge to a pin.
    allow_auto_tag = BooleanField(default=True)
    # Comma-separated keywords/phrases used by the keyword auto-tagger in addition to the badge name.
    keywords = TextField(null=True, blank=True)

    # NULL = global tag visible to all users; non-null = owned by one user.
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_tags",
    )

    # Hierarchical parents - symmetrical=False so parent→child is one direction.
    parents: ManyToManyField[Badge, Badge] = ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="children",
    )

    if TYPE_CHECKING:
        profile_id: int | None
        pins: ManyToManyField[Pin, Pin]
        locations: ManyToManyField[Location, Location]

    objects = BadgeManager()

    def _get_customization(self) -> BadgeCustomization | None:
        """Return this user's customization, if the queryset was prefetched."""
        cached: list[BadgeCustomization] = getattr(self, "_user_customizations", [])
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
    def initial_order_for_parents(
        cls,
        profile: Profile,
        parent_ids: list[str] | list[int],
    ) -> int | None:
        """Return ``order`` for a new badge placed just above its highest-priority parent.

        When parents are chosen at creation time, the new badge is placed immediately
        above the highest-priority parent among them. When multiple parents are
        selected, the parent with the smallest ``order`` value is used (e.g.
        Hospital at order 20 rather than Pennsylvania at order 35). The new badge
        receives that parent's ``order`` minus one (20 → 19).

        Args:
            profile: Owner profile used to resolve visible parent badges.
            parent_ids: Primary keys of selected parent badges.

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
    def get_badge_and_descendants(cls, badge_id: int) -> set[int]:
        """Return badge_id plus all descendant badge IDs (BFS, cycle-safe).

        Used so that filtering pins by a parent badge also surfaces pins carrying
        any of its descendant badges.
        """
        visited: set[int] = set()
        queue: list[int] = [badge_id]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            children_ids = list(cls.objects.filter(parents__id=current).values_list("id", flat=True))
            queue.extend(children_ids)
        return visited

    def __str__(self) -> str:
        if self.profile_id:
            return f"{self.name} ({self.profile})"
        return f"{self.name} [global]"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_tags"
        ordering = ["-order", "name"]
        get_latest_by = "updated"
        permissions = [("edit_global_badge", "Can edit global badges")]
        indexes = [
            Index(fields=["uuid"], name="idxdb_badge_uuid"),
            Index(fields=["profile"], name="idxdb_badge_profile"),
            Index(fields=["profile", "order"], name="idxdb_badge_pfile_ord"),
        ]
