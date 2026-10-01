"""Field-level revision history for Wiki. One row per field written."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index

from urbanlens.dashboard.models.abstract.versioned import AbstractFieldRevision


class WikiFieldRevision(AbstractFieldRevision):
    """One recorded write of one Wiki field.

    Attributes:
        target: The wiki the write landed on.
    """

    target = ForeignKey("dashboard.Wiki", on_delete=CASCADE, related_name="field_revisions")

    if TYPE_CHECKING:
        target_id: int

    def __str__(self) -> str:
        return f"{self.field_name}@{self.pk} on wiki {self.target_id} ({self.source})"

    class Meta(AbstractFieldRevision.Meta):
        db_table = "dashboard_wiki_field_revisions"
        ordering = ["target", "-id"]
        indexes = [
            # Serves the resolver directly: filter by target, narrow by source
            # or actor, then DISTINCT ON (field_name) ORDER BY id DESC.
            Index(fields=["target", "field_name", "-id"], name="idxdb_wikirev_tgt_fld_id"),
            Index(fields=["target", "source"], name="idxdb_wikirev_tgt_source"),
            Index(fields=["target", "actor"], name="idxdb_wikirev_tgt_actor"),
        ]
