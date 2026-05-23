"""Tag model - a named label applied to pins, with optional user ownership and hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, ImageField, Index, IntegerField, ManyToManyField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.tags.queryset import TagManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


ICON_CHOICES = [
    ("bookmark", "Bookmark"),
    ("star", "Star"),
    ("heart", "Heart"),
    ("flag", "Flag"),
    ("camera", "Camera"),
    ("home", "Home"),
    ("place", "Place"),
    ("explore", "Explore"),
    ("hiking", "Hiking"),
    ("warning", "Warning"),
    ("check_circle", "Check Circle"),
    ("schedule", "Schedule"),
    ("visibility", "Visibility"),
    ("lock", "Private"),
    ("archive", "Archive"),
    ("label", "Label"),
    ("local_offer", "Tag"),
    ("category", "Category"),
]

COLOR_CHOICES = [
    ("#F44336", "Red"),
    ("#E91E63", "Pink"),
    ("#9C27B0", "Purple"),
    ("#673AB7", "Deep Purple"),
    ("#3F51B5", "Indigo"),
    ("#2196F3", "Blue"),
    ("#03A9F4", "Light Blue"),
    ("#00BCD4", "Cyan"),
    ("#009688", "Teal"),
    ("#4CAF50", "Green"),
    ("#8BC34A", "Light Green"),
    ("#CDDC39", "Lime"),
    ("#FFEB3B", "Yellow"),
    ("#FFC107", "Amber"),
    ("#FF9800", "Orange"),
    ("#FF5722", "Deep Orange"),
    ("#795548", "Brown"),
    ("#607D8B", "Blue Grey"),
    ("#9E9E9E", "Grey"),
]


class Tag(abstract.Model):
    """A named label that can be applied to pins.

    Tags are either global (profile=None, visible to all users) or user-specific
    (profile set, only visible to that user and alongside global tags).

    Tags form an arbitrary-depth hierarchy via the parents M2M. Filtering by a tag
    also matches any descendant tags (use get_tag_and_descendants for the full set).

    Tags absorb the functionality of the former PinList model: they carry an icon,
    custom icon, color, description, and ordering weight that feeds into
    Pin.effective_icon's priority chain.
    """

    # NULL = global tag visible to all users; non-null = owned by one user.
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_tags",
    )
    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    # Hex color string chosen from COLOR_CHOICES (e.g. "#2196F3").
    color = CharField(max_length=50, null=True, blank=True, choices=COLOR_CHOICES)
    icon = CharField(max_length=50, null=True, blank=True, choices=ICON_CHOICES)
    custom_icon = ImageField(upload_to="tag_icons/", null=True, blank=True)
    # Higher order = checked first in the icon priority chain.
    order = IntegerField(default=0)
    # Hierarchical parents - symmetrical=False so parent→child is one direction.
    parents = ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="children",
    )

    objects = TagManager()

    if TYPE_CHECKING:
        profile_id: int | None

    @classmethod
    def get_tag_and_descendants(cls, tag_id: int) -> set[int]:
        """Return tag_id plus all descendant tag IDs (BFS, cycle-safe).

        Used so that filtering pins by a parent tag also surfaces pins carrying
        any of its descendant tags.
        """
        visited: set[int] = set()
        queue: list[int] = [tag_id]
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
        indexes = [
            Index(fields=["profile"]),
            Index(fields=["profile", "order"]),
        ]
