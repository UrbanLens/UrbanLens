"""BadgeCustomization model - per-user display overrides for global badges."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.model import Badge
    from urbanlens.dashboard.models.profile.model import Profile


class BadgeCustomization(abstract.Model):
    """Stores a user's display overrides for a global badge (tag or category).

    Each field is nullable - null means "use the badge's global value", non-null
    means "override with this value".  The form normalises empty strings to None
    before saving, so there is no ambiguity between "not set" and "cleared".
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="badge_customizations",
    )
    badge = ForeignKey(
        "dashboard.Badge",
        on_delete=CASCADE,
        related_name="customizations",
        db_column="tag_id",
    )
    # Null = use global value.  Non-null = override.
    name = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=50, null=True, blank=True)
    color = CharField(max_length=50, null=True, blank=True)

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_tag_customizations"
        constraints = [
            UniqueConstraint(
                fields=["profile", "badge"],
                name="unique_tag_customization_per_profile",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.profile} → {self.badge}"
