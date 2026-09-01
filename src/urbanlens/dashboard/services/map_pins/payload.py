"""Efficient, bounded map-pin payload generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.files.storage import default_storage
from django.db.models import Count, OuterRef, Prefetch, QuerySet, Subquery

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.images.relevance import MediaRelevance
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.reviews.model import Review

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.profile.model import Profile

#: Label kinds shown as chips on a pin. Excludes ``user`` (people labels) and
#: ``media`` (photo-only labels), neither of which describes the place itself.
DISPLAY_LABEL_KINDS = frozenset({"tag", "category", "status"})


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
        self._irrelevant_item_keys: set[str] | None = None

    def _irrelevant_item_keys_for_profile(self) -> set[str]:
        """This profile's own "not relevant" votes, computed once and cached on this instance.

        Only a materialized community-gallery photo (``media_item_key`` set)
        can appear here - see ``services.media.media_relevance.effective_relevance``'s
        own docs on why a plain personal upload is trusted by default instead.
        """
        if self._irrelevant_item_keys is None:
            self._irrelevant_item_keys = set(MediaRelevance.objects.filter(profile=self.profile, is_relevant=False).values_list("item_key", flat=True))
        return self._irrelevant_item_keys

    def prepare_queryset(self, query: QuerySet[Pin]) -> QuerySet[Pin]:
        latest_rating = Review.objects.filter(pin_id=OuterRef("pk")).order_by("-created").values("rating")[:1]
        # Fallback cover photo when the pin has none set explicitly: its own
        # earliest photo that this profile hasn't voted irrelevant. Annotated as
        # raw storage paths (not a second query per pin) so page()/all() stay a
        # single query regardless of how many pins are being built.
        fallback_photo = Image.objects.filter(pin_id=OuterRef("pk"), media_type=MediaKind.PHOTO).exclude(media_item_key__in=self._irrelevant_item_keys_for_profile()).order_by("created")
        return (
            # location__wiki as well as location: every pin serialized here reads
            # effective_name, which falls through to Location.display_name, which reads
            # the reverse OneToOne `wiki` - one query per pin on the map's own payload
            # unless it is joined in. That property's docstring asks callers to do this;
            # this is the highest-traffic caller in the app.
            query.select_related("location", "location__wiki", "cover_photo")
            .annotate(
                map_rating=Subquery(latest_rating),
                child_count=Count("detail_pins", distinct=True),
                fallback_photo_thumbnail=Subquery(fallback_photo.values("thumbnail")[:1]),
                fallback_photo_image=Subquery(fallback_photo.values("image")[:1]),
            )
            .prefetch_related(Prefetch("labels", queryset=Label.objects.with_customizations_for(self.profile)))
            .order_by("pk")
        )

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

    def display_labels(self, pin: Pin) -> list[Label]:
        """The pin's labels that display as chips, in prefetch order.

        Reads ``pin.labels.all()``, which ``prepare_queryset`` prefetches with
        the profile's per-label customizations applied - so calling this on a
        prepared pin costs no additional query. Shared with
        ``services.pins.pin_sync.serialize_sync_pin``, which needs the same set to
        emit each chip's ``kind``.

        Args:
            pin: The pin whose labels to filter. Should come from a queryset
                prepared by :meth:`prepare_queryset`, or this triggers a query.

        Returns:
            The pin's tag, category, and status labels.
        """
        return [label for label in pin.labels.all() if label.kind in DISPLAY_LABEL_KINDS]

    def serialize(self, pin: Pin) -> dict[str, Any]:
        labels = list(pin.labels.all())
        statuses = [b for b in labels if b.kind == "status"]
        categories = [b.name for b in labels if b.kind == "category"]
        # Include all display-relevant label kinds as chips so every label shows in the popup.
        # Status and category labels were previously omitted, causing them to be invisible.
        display_labels = self.display_labels(pin)
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
            "child_count": getattr(pin, "child_count", 0) or 0,
            "cover_photo_url": self._cover_photo_url(pin),
        }

    @staticmethod
    def _cover_photo_url(pin: Pin) -> str | None:
        """The popup thumbnail: the pin's explicit cover photo, else its fallback (see prepare_queryset)."""
        if pin.cover_photo is not None:
            return pin.cover_photo.thumb_url
        path = getattr(pin, "fallback_photo_thumbnail", None) or getattr(pin, "fallback_photo_image", None)
        return default_storage.url(path) if path else None

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
