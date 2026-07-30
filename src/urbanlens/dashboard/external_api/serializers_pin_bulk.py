"""Request/response types for multi-pin bulk actions: delete, merge, edit.

Mirrors ``dashboard/controllers/pin_bulk.py`` (the main map's multi-select
toolbar), with two deliberate departures from that internal surface, both
made for consistency with the rest of this API rather than with the page the
logic was copied from:

- Every optional field here follows the same "absent means untouched, an
  explicit null clears it" rule :class:`~urbanlens.dashboard.external_api.serializers.PinUpdateSerializer`
  documents, rather than the internal view's ``if truthy: apply`` checks
  (which have no way to clear a description or a rating in bulk at all).
- ``add_label_uuids``/``remove_label_uuids`` uuids must resolve to a label the
  caller may use, or the whole request is refused with 400 - the internal
  view silently drops unknown ids instead, which is fine for a multi-select
  UI built from the caller's own visible options but wrong for an API caller
  who may have sent a stale or mistyped uuid.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.external_api.serializers import PinSummarySerializer


class PinBulkDeleteSerializer(serializers.Serializer):
    """Validates a bulk pin-delete request."""

    uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)


class PinBulkDeleteResponseSerializer(serializers.Serializer):
    """The outcome of a bulk delete (schema-only).

    ``undo_uuid`` restores every deleted pin (and any child pins swept up with
    it) via the existing generic undo surface - ``POST
    undo/{undo_uuid}/restore/`` - rather than a bespoke restore endpoint of
    its own.
    """

    deleted = serializers.IntegerField(read_only=True)
    #: Child pins removed as part of deleting their selected ancestor, over
    #: and above the pins the caller explicitly named.
    descendant_count = serializers.IntegerField(read_only=True)
    total_count = serializers.IntegerField(read_only=True)
    undo_uuid = serializers.UUIDField(read_only=True)


class PinBulkMergeSerializer(serializers.Serializer):
    """Validates a bulk pin-merge request.

    ``target_uuid`` becomes (or stays) the top-level pin; every resolvable
    ``source_uuids`` entry becomes one of its detail pins.
    """

    target_uuid = serializers.UUIDField()
    source_uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)


class PinBulkMergeResponseSerializer(serializers.Serializer):
    """The outcome of a bulk merge (schema-only)."""

    target = PinSummarySerializer(read_only=True)
    merged_uuids = serializers.ListField(child=serializers.UUIDField(), read_only=True)
    #: Requested sources that were not merged because doing so would have
    #: made the target its own descendant.
    skipped_uuids = serializers.ListField(child=serializers.UUIDField(), read_only=True)


class PinBulkEditSerializer(serializers.Serializer):
    """Validates a bulk pin-edit request. Every field but ``uuids`` is optional.

    ``rating`` writes/clears a :class:`~urbanlens.dashboard.models.reviews.model.Review`
    for the caller against each selected pin, the same underlying write
    ``PUT``/``DELETE`` ``pins/{pin_slug}/review/`` makes one pin at a time.
    """

    uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)
    description = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    rating = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=5)
    #: Delta, not a replacement: labels not named here are left alone on every
    #: selected pin. Contrast ``PinUpdateSerializer.label_uuids``, which
    #: replaces one pin's complete set - a full-replacement doesn't generalize
    #: across pins that don't already share the same labels.
    add_label_uuids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True)
    remove_label_uuids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True)
    #: Reparents every selected pin under one target. Null detaches them to
    #: top-level.
    parent_uuid = serializers.UUIDField(required=False, allow_null=True)


class PinBulkEditResponseSerializer(serializers.Serializer):
    """The outcome of a bulk edit (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    #: Of ``count``, how many were actually reparented - a pin named as both a
    #: selected pin and (transitively) its own new parent is skipped.
    reparented = serializers.IntegerField(read_only=True)
