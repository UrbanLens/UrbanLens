"""LabelCustomization model - per-user display overrides for global labels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.customization.queryset import LabelCustomizationManager
from urbanlens.dashboard.services.core.colors import clean_color


class LabelCustomization(abstract.DashboardModel):
    """Stores a user's display overrides for a global label (tag or category).

    Each field is nullable - null means "use the label's global value", non-null
    means "override with this value".  The form normalises empty strings to None
    before saving, so there is no ambiguity between "not set" and "cleared".
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="label_customizations",
    )
    label = ForeignKey(
        "dashboard.Label",
        on_delete=CASCADE,
        related_name="customizations",
        db_column="label_id",
    )
    # Null = use global value.  Non-null = override.
    name = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=50, null=True, blank=True)
    color = CharField(max_length=50, null=True, blank=True)

    if TYPE_CHECKING:
        profile_id: int
        label_id: int

    def coerce_colors(self) -> None:
        """Drop `color` to NULL unless it is a colour this application stores.

        Weaker than `Label.color` to begin with - this column carries no
        `choices` - and it wins over the label's own value in
        `Label.effective_color`, so it is the one that actually renders.
        """
        self.color = clean_color(self.color, default=None)

    def save(self, *args, **kwargs) -> None:
        """Persist the override, coercing its colour first."""
        self.coerce_colors()
        super().save(*args, **kwargs)

    objects = LabelCustomizationManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_label_customizations"
        constraints = [
            UniqueConstraint(
                fields=["profile", "label"],
                name="unique_label_customization_per_profile",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.profile} → {self.label}"
