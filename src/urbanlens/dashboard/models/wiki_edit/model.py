"""WikiEdit model - community edit history for Wiki fields."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, ForeignKey, Index, JSONField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.wiki_edit.queryset import WikiEditManager

logger = logging.getLogger(__name__)


class WikiEdit(abstract.DashboardModel):
    """A single community edit applied to a Wiki's editable fields.

    Each edit stores the set of field changes as a JSON diff:
        {"name": {"from": "Old Name", "to": "New Name"}, ...}

    Reverts are implemented as new WikiEdit rows (so they appear in history)
    that carry the inverted diff, with ``reverted_by`` pointing at the edit being
    undone.

    Editable fields: name, description, security levels, dates. Coordinates
    are not editable - a Wiki's Location is fixed at creation. Bounding-box
    changes are stored as WKT strings under the key "bounding_box".
    """

    # {"field": {"from": old_val, "to": new_val}, ...}
    changes = JSONField()
    # True when this edit has been superseded by a revert.
    reverted = BooleanField(default=False)

    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        related_name="edits",
    )
    editor = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wiki_edits",
    )
    # The edit that reverted this one (filled in on the *target* edit when someone reverts it).
    reverted_by = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="reverts",
    )

    if TYPE_CHECKING:
        wiki_id: int
        editor_id: int | None
        reverted_by_id: int | None

    objects = WikiEditManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_wiki_edits"
        ordering = ["-created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["wiki"], name="idxdb_we_wiki"),
            Index(fields=["wiki", "created"], name="idxdb_we_created"),
        ]
