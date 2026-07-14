"""Efficient, bounded map-pin payload generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.models import OuterRef, Prefetch, QuerySet, Subquery

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.reviews.model import Review

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True)
class MapPinPage:
    pins: list[dict[str, Any]]
    next_cursor: int | None
    total: int | None = None


class MapPinPayloadService:
    """Build map pin JSON in small, database-only batches.

    The map endpoint is intentionally different from rich pin-detail serializers:
    it avoids geocoding-backed properties, avoids per-pin review queries, and
    supports keyset pagination so one large user cannot monopolize a worker.
    """

    DEFAULT_LIMIT = 500
    MAX_LIMIT = 1000

    def __init__(self, profile: Profile):
        self.profile = profile

    def prepare_queryset(self, query: QuerySet[Pin]) -> QuerySet[Pin]:
        latest_rating = Review.objects.filter(pin_id=OuterRef("pk")).order_by("-created").values("rating")[:1]
        return query.select_related("location").annotate(map_rating=Subquery(latest_rating)).prefetch_related(Prefetch("labels", queryset=Label.objects.with_customizations_for(self.profile))).order_by("pk")

    def page(self, query: QuerySet[Pin], *, cursor: int | None = None, limit: int | None = None, include_total: bool = False) -> MapPinPage:
        limit = min(max(int(limit or self.DEFAULT_LIMIT), 1), self.MAX_LIMIT)
        if cursor:
            query = query.filter(pk__gt=cursor)
        total = query.count() if include_total else None
        rows = list(self.prepare_queryset(query)[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        pins = [self.serialize(pin) for pin in rows]
        next_cursor = rows[-1].pk if has_more and rows else None
        return MapPinPage(pins=pins, next_cursor=next_cursor, total=total)

    def all(self, query: QuerySet[Pin]) -> list[dict[str, Any]]:
        return [self.serialize(pin) for pin in self.prepare_queryset(query).iterator(chunk_size=1000)]

    def serialize(self, pin: Pin) -> dict[str, Any]:
        labels = list(pin.labels.all())
        statuses = [b for b in labels if b.kind == "status"]
        categories = [b.name for b in labels if b.kind == "category"]
        # Include all display-relevant label kinds as chips so every label shows in the popup.
        # Status and category labels were previously omitted, causing them to be invisible.
        display_labels = [b for b in labels if b.kind in {"tag", "category", "status"}]
        return {
            "id": pin.pk,
            "uuid": str(pin.uuid),
            "slug": pin.slug or str(pin.uuid),
            "name": pin.effective_name,
            "icon": self._effective_icon(pin, labels),
            "description": pin.description or "",
            "priority": pin.priority,
            "last_visited": pin.last_visited.isoformat() if pin.last_visited else "never",
            "latitude": pin.effective_latitude,
            "longitude": pin.effective_longitude,
            "status": statuses[0].name if statuses else "",
            "categories": categories,
            "profile": pin.profile_id,
            "rating": getattr(pin, "map_rating", None) or 0,
            "color": self._effective_color(pin, labels),
            "tags": [{"id": t.id, "name": t.name, "color": t.effective_color, "icon": t.effective_icon} for t in display_labels],
            "address": pin.effective_address,
            # The pin's own icon/color overrides, distinct from "icon"/"color" above
            # (which fall back to an inherited label's icon/color for map display).
            # The edit dialog must pre-fill from these, not the effective values -
            # otherwise resaving a pin that merely *displays* a label's icon bakes
            # that icon onto the pin permanently, even though the user never touched it.
            "own_icon": pin.icon,
            "own_custom_icon_url": pin.custom_icon.url if pin.custom_icon else None,
            "own_color": pin.color,
        }

    @staticmethod
    def _ordered_location_labels(labels: list[Label]) -> list[Label]:
        return sorted((b for b in labels if b.kind != "user"), key=lambda b: (-b.order, b.name or ""))

    def _winning_display_label(self, pin: Pin, labels: list[Label]) -> Label | None:
        if pin.custom_icon or pin.icon:
            return None
        for label in self._ordered_location_labels(labels):
            if label.custom_icon and not label.icon_is_overridden:
                return label
            if label.effective_icon:
                return label
        return None

    def _effective_icon(self, pin: Pin, labels: list[Label]) -> str | None:
        if pin.custom_icon:
            return pin.custom_icon.url
        if pin.icon:
            return pin.icon
        winning = self._winning_display_label(pin, labels)
        if not winning:
            return None
        if winning.custom_icon and not winning.icon_is_overridden:
            return winning.custom_icon.url
        return winning.effective_icon

    def _effective_color(self, pin: Pin, labels: list[Label]) -> str | None:
        if pin.color:
            return pin.color
        if pin.custom_icon or pin.icon:
            return None
        winning = self._winning_display_label(pin, labels)
        return winning.effective_color if winning else None
