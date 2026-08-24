"""Field-level revision history for Wiki.

One row per field written. See ``models/abstract/versioned.py`` for the shape
and ``docs/designs/versioned-content.md`` for why it exists - the short version
is that a concealed viewer must be shown automatic writes plus their own plus
their friends', which is a different subset for every viewer and therefore
cannot be a materialised projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, Index, UniqueConstraint

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
        return f"{self.field_name}@{self.sequence} on wiki {self.target_id} ({self.source})"

    class Meta(AbstractFieldRevision.Meta):
        db_table = "dashboard_wiki_field_revisions"
        ordering = ["target", "-sequence"]
        constraints = [
            UniqueConstraint(fields=["target", "sequence"], name="uniq_wiki_revision_sequence"),
        ]
        indexes = [
            # Serves the resolver directly: filter by target, narrow by source
            # or actor, then DISTINCT ON (field_name) ORDER BY sequence DESC.
            Index(fields=["target", "field_name", "-sequence"], name="idxdb_wikirev_tgt_fld_seq"),
            Index(fields=["target", "source"], name="idxdb_wikirev_tgt_source"),
            Index(fields=["target", "actor"], name="idxdb_wikirev_tgt_actor"),
        ]
