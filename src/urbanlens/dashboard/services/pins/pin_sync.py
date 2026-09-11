"""Delta-sync pages of a profile's pins and pin deletions, for external clients.
A sync client calls ``pins/`` repeatedly with the ``sync_watermark`` it was handed on its previous sync as ``modified_since``, pages through changed pins with the opaque cursor, then does the same against ``pins/deleted/`` for tombstones - after which its local copy matches the server without ever downloading unchanged rows."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_tombstone import PinTombstone
from urbanlens.dashboard.services.map_pins import MapPinPayloadService

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

#: Subtracted from "now" to produce ``sync_watermark`` - covers rows whose
#: transaction was in flight (stamped, not yet visible) while a sync ran.
SYNC_WATERMARK_GRACE = timedelta(seconds=10)

#: How long deletion tombstones are retained before the scheduled pruning task
#: (``tasks.prune_pin_tombstones``) removes them.
#: This is the longest supported sync-client offline gap: a client that hasn't synced within this
#: window can no longer trust the deletions feed incrementally (pruned tombstones are unrecoverable)
TOMBSTONE_RETENTION = timedelta(days=400)


class InvalidSyncCursorError(ValueError):
    """The supplied cursor is not one this service issued.
    The message is for logs, not the response: a caller's HTTP-facing code should catch this and author its own user-facing text, rather than relaying the exception's message - that keeps a future raise site here from being able to smuggle unreviewed text into a response just by adding a new ``raise``."""


class StaleDeletedSinceError(ValueError):
    """``deleted_since`` predates the tombstone retention floor.
    Tombstones older than :data:`TOMBSTONE_RETENTION` are pruned, so a client asking for deletions from before that floor could silently miss some - incremental sync is no longer trustworthy and the client must resync its pins from scratch (drop local rows absent from a full ``pins/`` walk)."""


@dataclass(frozen=True, slots=True)
class PinSyncPage:
    """One page of changed pins plus the pagination/watermark bookkeeping."""

    pins: list[dict[str, Any]]
    next_cursor: str | None
    sync_watermark: str
    total: int | None = None


@dataclass(frozen=True, slots=True)
class TombstoneSyncPage:
    """One page of pin deletions plus the pagination/watermark bookkeeping."""

    tombstones: list[dict[str, Any]]
    next_cursor: str | None
    sync_watermark: str


def _encode_cursor(stamp: datetime, pk: int) -> str:
    """Encode an ``(updated, pk)`` keyset position as an opaque URL-safe token."""
    return base64.urlsafe_b64encode(f"{stamp.isoformat()}|{pk}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Decode a cursor token back into its keyset position.

    Args:
        cursor: A token previously produced by :func:`_encode_cursor`.

    Returns:
        The ``(timestamp, pk)`` position the next page starts after.

    Raises:
        InvalidSyncCursorError: The token is malformed or was never ours."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp_raw, _, pk_raw = raw.rpartition("|")
        stamp = datetime.fromisoformat(stamp_raw)
        pk = int(pk_raw)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidSyncCursorError(f"cursor {cursor!r} failed to decode: {exc}") from exc
    if timezone.is_naive(stamp):
        raise InvalidSyncCursorError(f"cursor {cursor!r} decoded to a naive timestamp {stamp!r}")
    return stamp, pk


def _after_cursor(query: QuerySet, stamp: datetime, pk: int, *, stamp_field: str) -> QuerySet:
    """Keyset condition: rows strictly after ``(stamp, pk)`` in ``(stamp_field, pk)`` order."""
    return query.filter(Q(**{f"{stamp_field}__gt": stamp}) | Q(**{stamp_field: stamp, "pk__gt": pk}))


def _watermark() -> str:
    """The ``modified_since`` value a client should send on its next sync."""
    return (timezone.now() - SYNC_WATERMARK_GRACE).isoformat()


def sync_pins_page(
    profile: Profile,
    *,
    modified_since: datetime | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    include_total: bool = False,
) -> PinSyncPage:
    """Return one page of the profile's pins changed at or after ``modified_since``.
    Serves *all* of the profile's pins - root and detail/child alike - unlike the web map's root-only payload; a sync client mirrors the full dataset and reconstructs the hierarchy from each pin's ``parent_uuid``.

    Args:
        profile: The profile whose pins to page through.
        modified_since: Inclusive lower bound on ``updated``; ``None`` means
            a full sync from the beginning.
        cursor: Opaque continuation token from the previous page, if any.
        limit: Page size, clamped to ``MapPinPayloadService.MAX_LIMIT``.
        include_total: Also count every row matching the window (one extra
            query) - meant for a client's initial-sync progress bar.

    Returns:
        The page of serialized pins, ordered by ``(updated, pk)``.

    Raises:
        InvalidSyncCursorError: ``cursor`` is malformed or was never ours."""
    watermark = _watermark()
    limit = min(max(int(limit or MapPinPayloadService.DEFAULT_LIMIT), 1), MapPinPayloadService.MAX_LIMIT)

    query = Pin.objects.filter(profile=profile)
    if modified_since is not None:
        query = query.modified_since(modified_since)
    if cursor:
        query = _after_cursor(query, *_decode_cursor(cursor), stamp_field="updated")

    total = query.count() if include_total else None

    service = MapPinPayloadService(profile)
    rows = list(service.prepare_queryset(query).select_related("parent_pin").order_by("updated", "pk")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    pins = [serialize_sync_pin(service, pin) for pin in rows]
    next_cursor = _encode_cursor(rows[-1].updated, rows[-1].pk) if has_more and rows else None
    return PinSyncPage(pins=pins, next_cursor=next_cursor, sync_watermark=watermark, total=total)


def serialize_sync_pin(service: MapPinPayloadService, pin: Pin) -> dict[str, Any]:
    """The map payload shape plus the sync-only fields layered on top."""
    payload = service.serialize(pin)
    payload["pin_type"] = pin.pin_type
    # The map payload names its labels by id and sends the dictionary once per response; a sync
    # client holds one pin at a time and offline, so it gets the labels themselves - each already
    # carrying its id, which is why the ids are dropped rather than sent alongside.
    # Labels are already prefetched by prepare_queryset, so this adds no query.
    payload.pop("label_ids", None)
    chips = service.display_labels(pin)
    payload["tags"] = [{"id": label.id, "name": label.name, "color": label.effective_color, "icon": label.effective_icon, "kind": label.kind} for label in chips]
    statuses = [label.name for label in chips if label.kind == "status"]
    payload["status"] = statuses[0] if statuses else ""
    payload["categories"] = [label.name for label in chips if label.kind == "category"]
    parent = pin.parent_pin
    payload["parent_uuid"] = str(parent.uuid) if parent is not None else None
    payload["created"] = pin.created.isoformat()
    payload["updated"] = pin.updated.isoformat()
    return payload


def sync_tombstones_page(
    profile: Profile,
    *,
    deleted_since: datetime | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> TombstoneSyncPage:
    """Return one page of the profile's pin deletions at or after ``deleted_since``.

    Args:
        profile: The profile whose deletions to page through.
        deleted_since: Inclusive lower bound on the deletion time; ``None``
            returns every retained tombstone (a client doing its very first
            sync doesn't need any - it holds nothing to delete).
        cursor: Opaque continuation token from the previous page, if any.
        limit: Page size, clamped to ``MapPinPayloadService.MAX_LIMIT``.

    Returns:
        The page of deletions, ordered by ``(created, pk)``, each as
        ``{"pin_uuid": ..., "deleted_at": ...}``.

    Raises:
        InvalidSyncCursorError: ``cursor`` is malformed or was never ours.
        StaleDeletedSinceError: ``deleted_since`` predates the tombstone
            retention floor - pruning may have removed deletions the client
            never saw, so it must full-resync instead (HTTP 410 upstream).
    """
    if deleted_since is not None and deleted_since < timezone.now() - TOMBSTONE_RETENTION:
        raise StaleDeletedSinceError(f"deleted_since={deleted_since.isoformat()} is older than TOMBSTONE_RETENTION ({TOMBSTONE_RETENTION.days} days) for profile {profile.pk}; tombstones this old may already be pruned.")
    watermark = _watermark()
    limit = min(max(int(limit or MapPinPayloadService.DEFAULT_LIMIT), 1), MapPinPayloadService.MAX_LIMIT)

    query = PinTombstone.objects.for_profile(profile)
    if deleted_since is not None:
        query = query.deleted_since(deleted_since)
    if cursor:
        query = _after_cursor(query, *_decode_cursor(cursor), stamp_field="created")

    rows = list(query.order_by("created", "pk")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    tombstones = [{"pin_uuid": str(row.pin_uuid), "deleted_at": row.created.isoformat()} for row in rows]
    next_cursor = _encode_cursor(rows[-1].created, rows[-1].pk) if has_more and rows else None
    return TombstoneSyncPage(tombstones=tombstones, next_cursor=next_cursor, sync_watermark=watermark)
