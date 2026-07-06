"""Alias models - alternate names for Pins (personal) and Locations (shared)."""

from __future__ import annotations

import logging

from django.db.models import CASCADE, SET_NULL, ForeignKey, Index, UniqueConstraint
from django.db.models.fields import CharField

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class _AliasBase(abstract.Model):
    """Shared fields for all alias types."""

    name = CharField(max_length=255)

    class Meta(abstract.Model.Meta):
        abstract = True
        ordering = ["name"]


class PinAlias(_AliasBase):
    """An alternate name for a Pin, visible only to the pin's owner.

    Ownership is derived from pin.profile; no separate profile FK is needed.
    The unique constraint prevents duplicate alias names on the same pin.
    """

    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="aliases",
    )

    def __str__(self) -> str:
        return f"{self.name} (pin alias)"

    class Meta(_AliasBase.Meta):
        db_table = "dashboard_pin_aliases"
        indexes = [
            Index(fields=["pin"], name="idxdb_pin_alias_pin"),
        ]
        constraints = [
            UniqueConstraint(fields=["pin", "name"], name="db_pin_alias_unique"),
        ]


class LocationAlias(_AliasBase):
    """An alternate name for a Location, visible to all users who have it pinned.

    ``created_by`` is optional attribution only - deleting a profile does not
    cascade-delete the alias.
    """

    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="aliases",
    )
    created_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="location_aliases_created",
    )

    def __str__(self) -> str:
        return f"{self.name} (location alias)"

    class Meta(_AliasBase.Meta):
        db_table = "dashboard_location_aliases"
        indexes = [
            Index(fields=["location"], name="idxdb_loc_alias_loc"),
        ]
        constraints = [
            UniqueConstraint(fields=["location", "name"], name="db_loc_alias_unique"),
        ]
