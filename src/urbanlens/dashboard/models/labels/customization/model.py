"""LabelCustomization model - per-user display overrides for global labels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.customization.queryset import LabelCustomizationManager
from urbanlens.dashboard.services.core.colors import clean_color
from urbanlens.dashboard.services.core.icons import clean_icon
from urbanlens.dashboard.services.core.text_limits import column_max_length


class LabelCustomization(abstract.DashboardModel):
    """Per-user display overrides for a global label (null = use global value)."""

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
    name = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=50, null=True, blank=True)
    color = CharField(max_length=50, null=True, blank=True)

    if TYPE_CHECKING:
        profile_id: int
        label_id: int

    def coerce_colors(self) -> None:
        """Drop `color` to NULL unless it is a valid colour."""
        self.color = clean_color(self.color, default=None)

    def coerce_icon(self) -> None:
        """Drop `icon` to NULL unless it is an icon shape `clean_icon` accepts."""
        self.icon = clean_icon(self.icon, max_length=column_max_length(LabelCustomization, "icon"))

    def save(self, *args, **kwargs) -> None:
        """Persist the override, coercing its colour and icon first."""
        self.coerce_colors()
        self.coerce_icon()
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
