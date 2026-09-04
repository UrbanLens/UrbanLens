"""WikiEdit model - community edit history for Wiki fields."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, ForeignKey, Index, JSONField, PositiveSmallIntegerField

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
    # True when this edit IS a revert of another one. Undoing somebody's work is
    # not itself a contribution to pay for: paying both sides of an edit war was
    # a standing invitation to farm points by reverting back and forth. Stored
    # rather than derived from `reverts` because the award decision happens in a
    # post_save handler, before the reverting row's `reverted_by` back-reference
    # on the target has been written.
    is_revert = BooleanField(default=False)
    # What this row actually paid its editor, and whether that payment has since
    # been taken back. Recorded rather than recomputed on demand because the
    # weights in services.consensus.points are a first cut expected to be
    # retuned, and a retraction has to return exactly what was paid, not what
    # the same edit would earn today. `consensus_points_retracted` is the
    # compare-and-swap flag that makes retraction idempotent - the same shape
    # ReputationEvent.retracted uses, and for the same reason: reverting a
    # revert has to put the points back.
    consensus_points = PositiveSmallIntegerField(default=0)
    consensus_points_retracted = BooleanField(default=False)

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
    # Set only when this edit was produced by the Consensus game
    # (services.consensus.session), never by a manual edit. Doubles as the
    # double-award guard for models.wiki_edit.signals's points hook - a
    # Consensus-sourced edit already got its (larger, in-game) points at
    # resolution time, so the generic "any wiki edit earns baseline points"
    # signal must skip it - and as attribution for the wiki-history UI.
    consensus_round = ForeignKey(
        "dashboard.ConsensusRound",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wiki_edits",
    )

    if TYPE_CHECKING:
        wiki_id: int
        editor_id: int | None
        reverted_by_id: int | None
        consensus_round_id: int | None

    objects = WikiEditManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_wiki_edits"
        ordering = ["-created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["wiki", "created"], name="idxdb_we_created"),
        ]
