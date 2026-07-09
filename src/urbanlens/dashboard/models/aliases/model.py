"""Alias models - alternate names for Pins (personal) and Wikis (shared)."""

from __future__ import annotations

import logging

from django.db.models import CASCADE, SET_NULL, ForeignKey, Index, TextChoices, UniqueConstraint
from django.db.models.fields import CharField

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class AliasType(TextChoices):
    """
    The type of alias.
    * NICKNAME: A user-defined nickname for the pin or wiki.
                Created by checking the "nickname" checkbox when adding an alias.
    * OFFICIAL: An official name for the pin or location.
                Created by the system when the pin or location is created, or queried from an external API source.
    * ALTERNATE: An alternate name for the pin or location.
                Created by the user when adding an alias. (without the "nickname" checkbox)
    """

    NICKNAME = "nickname", "Nickname"
    OFFICIAL = "official", "Official Name"
    ALTERNATE = "alternate", "Alternate Name"


class AliasSource:
    """Well-known alias ``source`` values.

    ``source`` is a free-text slug so plugin name providers can attribute
    aliases to themselves (e.g. ``"google_places"``, ``"wikipedia"``) without
    the model enumerating every provider. These constants cover the two
    non-plugin origins.

    * USER: A user-defined alias for the pin or location.
    * OTHER: An alias whose external origin is unknown (e.g. backfilled data).
    """

    USER = "user"
    OTHER = "other"


class _AliasBase(abstract.DashboardModel):
    """Shared fields for all alias types."""

    name = CharField(max_length=255)
    kind = CharField(max_length=10, choices=AliasType.choices, default=AliasType.ALTERNATE)
    # Free-text slug, not choices: plugin name providers write their own source slugs.
    source = CharField(max_length=50, default=AliasSource.USER)

    class Meta(abstract.DashboardModel.Meta):
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
            Index(fields=["pin"], name="idxdb_palias_pin"),
            Index(fields=["pin", "kind"], name="idxdb_palias_pin_kind"),
            Index(fields=["pin", "source"], name="idxdb_palias_pin_source"),
        ]
        constraints = [
            UniqueConstraint(fields=["pin", "name"], name="db_pin_alias_unique"),
        ]


class WikiAlias(_AliasBase):
    """An alternate name for a Wiki, visible to all users who have its place pinned.

    ``created_by`` is optional attribution only - deleting a profile does not
    cascade-delete the alias.
    """

    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        related_name="aliases",
    )
    created_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wiki_aliases_created",
    )

    def __str__(self) -> str:
        return f"{self.name} (wiki alias)"

    class Meta(_AliasBase.Meta):
        db_table = "dashboard_wiki_aliases"
        indexes = [
            Index(fields=["wiki"], name="idxdb_walias_wiki"),
            Index(fields=["wiki", "kind"], name="idxdb_walias_wiki_kind"),
            Index(fields=["wiki", "source"], name="idxdb_walias_wiki_source"),
        ]
        constraints = [
            UniqueConstraint(fields=["wiki", "name"], name="db_walias_unique"),
        ]
