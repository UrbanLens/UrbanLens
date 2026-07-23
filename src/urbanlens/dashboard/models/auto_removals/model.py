"""Tombstones for user-deleted auto-added items (aliases, links, labels, owners).

Several kinds of Pin/Wiki sub-records can be created either by the user
directly, or automatically by background/on-demand code (external name-provider
syncs, AI link/alias extraction, keyword/AI auto-tagging, ...). Before this
model existed, deleting one of those auto-added records had no lasting effect:
the same automatic code path would run again later (the next panel view, the
next enrichment cycle, a re-run of AI extraction) and silently recreate the
exact thing the user just removed, since none of those paths had any way to
know the user had already rejected that value.

A row here means "the user removed this and it must not come back on its own."
Automatic-creation code should check ``was_removed()`` before creating a
record; manual delete views should call ``record()`` when the deleted record
was itself auto-added (a purely user-created-and-user-deleted record has
nothing to suppress - there's no automation that would ever recreate it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index, TextChoices, UniqueConstraint
from django.db.models.fields import CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.auto_removals.queryset import AutoRemovalManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki


class AutoRemovalKind(TextChoices):
    """The kind of auto-added sub-record a tombstone row suppresses."""

    ALIAS = "alias", "Alias"
    LINK = "link", "Link"
    LABEL = "label", "Label"
    OWNER = "owner", "Owner"


class _AutoRemovalBase(abstract.DashboardModel):
    """Shared fields: which kind of item, and its normalized identifying value.

    ``value`` is normalized per-kind before storage/lookup so a re-add attempt
    with cosmetically different text still matches: alias/owner names are
    lowercased (case-insensitive, matching the alias/owner uniqueness rules),
    label values are the label's primary key as a string, link values are the
    exact URL (case-sensitive by nature).
    """

    kind = CharField(max_length=10, choices=AutoRemovalKind.choices)
    value = CharField(max_length=500)

    objects: AutoRemovalManager = AutoRemovalManager()

    class Meta(abstract.DashboardModel.Meta):
        abstract = True


class PinAutoRemoval(_AutoRemovalBase):
    """Records that a profile deleted an auto-added item from one of their Pins."""

    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, related_name="auto_removals")

    if TYPE_CHECKING:
        pin_id: int

    def __str__(self) -> str:
        return f"{self.kind}:{self.value} removed from pin {self.pin_id}"

    class Meta(_AutoRemovalBase.Meta):
        db_table = "dashboard_pin_auto_removals"
        indexes = [
            Index(fields=["pin", "kind"], name="idxdb_pautorm_pin_kind"),
        ]
        constraints = [
            UniqueConstraint(fields=["pin", "kind", "value"], name="db_pin_auto_removal_unique"),
        ]


class WikiAutoRemoval(_AutoRemovalBase):
    """Records that a profile deleted an auto-added item from a community Wiki."""

    wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, related_name="auto_removals")

    if TYPE_CHECKING:
        wiki_id: int

    def __str__(self) -> str:
        return f"{self.kind}:{self.value} removed from wiki {self.wiki_id}"

    class Meta(_AutoRemovalBase.Meta):
        db_table = "dashboard_wiki_auto_removals"
        indexes = [
            Index(fields=["wiki", "kind"], name="idxdb_wautorm_wiki_kind"),
        ]
        constraints = [
            UniqueConstraint(fields=["wiki", "kind", "value"], name="db_wiki_auto_removal_unique"),
        ]
