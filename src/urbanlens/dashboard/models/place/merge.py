"""PlaceMerge - the audit record of one duplicate building place merged into another."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import SET_NULL, CharField, ForeignKey, JSONField, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract


class PlaceMerge(abstract.DashboardModel):
    """One loser place folded into its survivor by ``services.places.building_merge``.
    The loser is never deleted - it stays as a SUPERSEDED row with its provider key cleared - so this record is what ties it to the place that now stands for it, and the only home for what the merge could not keep: where both places (or their wikis) held a value for the same key, the survivor's stayed and the loser's is here.

    Attributes:
        survivor: The place that absorbed the loser.
        loser: The place merged away. At most one record per loser, which is what makes a re-run a no-op.
        gers_id: The Overture building both places stood for.
        old_keys: The loser's provider keys before the merge (``provider:key``).
        moved: What moved to the survivor - location, wiki and relation ids and counts.
        dropped: Per-key values the survivor already had a value for, and duplicate rows deduplicated away, each as a
            full snapshot (see ``services.core.row_merge.snapshot_row``).
    """

    survivor = ForeignKey("dashboard.Place", on_delete=SET_NULL, null=True, blank=True, related_name="merges_absorbed")
    loser = ForeignKey("dashboard.Place", on_delete=SET_NULL, null=True, blank=True, related_name="merges_as_loser")
    gers_id = CharField(max_length=64, blank=True, default="")
    old_keys = JSONField(default=list, blank=True)
    moved = JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    dropped = JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    if TYPE_CHECKING:
        survivor_id: int | None
        loser_id: int | None

    def __str__(self) -> str:
        return f"PlaceMerge(place {self.loser_id} -> {self.survivor_id}, {self.gers_id or 'no gers id'})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_place_merges"
        get_latest_by = "created"
        constraints = [
            UniqueConstraint(fields=["loser"], condition=Q(loser__isnull=False), name="place_merge_one_per_loser"),
        ]
