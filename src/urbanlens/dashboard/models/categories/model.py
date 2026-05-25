"""Category model - a global, hierarchical label applied to pins and locations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CharField, Index, IntegerField, ManyToManyField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.categories.queryset import CategoryManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class Category(abstract.Model):
    """A global, hierarchical label applied to pins and locations."""

    name = CharField(max_length=255, unique=True)
    description = TextField(null=True, blank=True)
    color = CharField(max_length=50, null=True, blank=True)
    icon = CharField(max_length=50, null=True, blank=True)
    order = IntegerField(default=0)
    parents = ManyToManyField("self", symmetrical=False, blank=True, related_name="children")

    objects = CategoryManager()

    def __str__(self) -> str:
        return self.name

    @classmethod
    def get_category_and_descendants(cls, category_id: int) -> list[int]:
        """Return category_id plus all descendant IDs (BFS, cycle-safe).

        Args:
            category_id: The ID of the root category.

        Returns:
            List of category IDs including the root and all descendants.
        """
        seen: set[int] = set()
        queue = [category_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            children = list(cls.objects.filter(parents__id=current).values_list("id", flat=True))
            queue.extend(children)
        return list(seen)

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_categories"
        ordering = ["-order", "name"]
        get_latest_by = "updated"
        indexes = [
            Index(fields=["order"], name="dashboard_category_order_idx"),
        ]
