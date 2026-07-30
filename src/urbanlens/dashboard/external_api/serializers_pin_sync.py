"""Request/response types for the manual pin <-> wiki child-marker sync.

The push body's bounds are the interesting part. ``child_pin_uuids`` is
``min_length=1`` because an empty list is never a meaningful request - the
internal controller answers 400 for it, and silently succeeding with
``created: 0`` would let a buggy client believe it had synced. It is
``max_length=500`` to match ``services.pin_wiki_sync.MAX_SYNC_ITEMS``, which the
service applies by *slicing*: without the bound here, a 10,000-entry list would
be accepted, quietly truncated to 500, and reported as a success the client
could not distinguish from a complete one.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.services.pin_wiki_sync import MAX_SYNC_ITEMS


class PinWikiSyncPushSerializer(serializers.Serializer):
    """Validates which of a pin's child pins to publish to its community wiki."""

    #: The child pins to send, by uuid. Only uuids that are actually children of
    #: the addressed pin are used - the view filters against
    #: ``pin.detail_pins``, so an id belonging to another pin (or another user)
    #: is dropped rather than refused, exactly as the internal toolbar behaves
    #: when a selection goes stale mid-interaction.
    child_pin_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=MAX_SYNC_ITEMS,
        allow_empty=False,
    )


class PinWikiSyncResultSerializer(serializers.Serializer):
    """What one sync call did (schema-only)."""

    #: Markers actually created. Zero is a normal outcome, not a failure: it
    #: means everything selected was already covered on the other side (or that
    #: ``wiki_exists`` is false).
    created = serializers.IntegerField(read_only=True)
    #: Whether the pin's location has a community wiki at all. Reported rather
    #: than 404'd because "this property has no wiki yet" is a state the client
    #: should render as an invitation to create one, not as an error - and a 404
    #: here would be indistinguishable from "no such pin".
    wiki_exists = serializers.BooleanField(read_only=True)
