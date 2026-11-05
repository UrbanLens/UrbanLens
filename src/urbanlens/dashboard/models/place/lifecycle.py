"""Place history: every outline a place has had, and the edges that tie a retired place to what replaced it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.gis.db.models import MultiPolygonField
from django.db.models import CASCADE, BooleanField, CharField, CheckConstraint, DateTimeField, F, ForeignKey, Index, Q, TextChoices, UniqueConstraint
from django.utils import timezone

from urbanlens.dashboard.models import abstract


class PlaceGeometryVersion(abstract.DashboardModel):
    """
    One outline a place has had. Rows are only ever appended.

    ``Place.geometry`` stays the current outline for queries; this table is how earlier outlines are found again,
    for example to notice that a parcel shrank and a neighbour appeared in the space it left.

    Attributes:
        place: The place this outline belonged to.
        geometry: The outline.
        recorded_at: When it was recorded.
        provider: Where it came from (a provider key, ``"vote"`` for a boundary vote, or blank for the backfill).
        erroneous: Set once a correction shows the outline was wrong. The row stays; split detection ignores it.
            It is the only field that changes after insert, and only through ``services.places.geometry_versions.mark_erroneous``.
    """

    place = ForeignKey("dashboard.Place", on_delete=CASCADE, related_name="geometry_versions")
    geometry = MultiPolygonField(geography=True, srid=4326)
    recorded_at = DateTimeField(default=timezone.now)
    provider = CharField(max_length=20, blank=True, default="")
    erroneous = BooleanField(default=False)

    if TYPE_CHECKING:
        place_id: int

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Insert only; a stored outline is never rewritten."""
        if not self._state.adding:
            msg = "PlaceGeometryVersion is append-only; flag a wrong outline erroneous and append the correction."
            raise ValueError(msg)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"GeometryVersion(place {self.place_id} at {self.recorded_at:%Y-%m-%d})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_place_geometry_versions"
        get_latest_by = "recorded_at"
        indexes = [Index(fields=["place", "recorded_at"], name="idxdb_geomversion_place")]


class LineageEdgeKind(TextChoices):
    """How a retired place relates to the one that continues it."""

    SPLIT_INTO = "split_into", "Split into"
    MERGED_INTO = "merged_into", "Merged into"
    REPLACED_BY = "replaced_by", "Replaced by"


class PlaceLineageEdge(abstract.DashboardModel):
    """
    A directed edge from a place that stopped describing the ground to one that carries on.
    Written only by ``services.places.lineage_events.handle_lineage_event``, alongside the access snapshot for the same event.

    Attributes:
        predecessor: The retired place.
        successor: The place that continues it.
        kind: How (see :class:`LineageEdgeKind`).
        learned_at: When the event was recorded; the same instant the access snapshot used.
        erroneous: Set when the event is shown to have been a mistake. The edge stays as a record.
    """

    predecessor = ForeignKey("dashboard.Place", on_delete=CASCADE, related_name="successor_edges")
    successor = ForeignKey("dashboard.Place", on_delete=CASCADE, related_name="predecessor_edges")
    kind = CharField(max_length=20, choices=LineageEdgeKind.choices)
    learned_at = DateTimeField(default=timezone.now)
    erroneous = BooleanField(default=False)

    if TYPE_CHECKING:
        predecessor_id: int
        successor_id: int

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: place {self.predecessor_id} -> {self.successor_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_place_lineage_edges"
        constraints = [
            UniqueConstraint(fields=["predecessor", "successor", "kind"], name="place_lineage_edge_unique"),
            CheckConstraint(condition=~Q(predecessor=F("successor")), name="place_lineage_edge_not_self"),
        ]
        indexes = [Index(fields=["successor"], name="idxdb_lineage_successor")]
