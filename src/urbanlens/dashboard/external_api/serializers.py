"""Deliberately minimal, hand-rolled serializers for the external API.

These never subclass or reuse the internal ``PinSerializer``/``ProfileSerializer``
(``dashboard/models/pin/serializer.py``, ``dashboard/models/profile/serializer.py``) -
the internal API is free to grow fields for the site's own frontend without
silently expanding what a third-party application is permitted to submit or
read. Field-level bounds here are the first line of defense against
untrusted input; ``services.pin_creation.create_pin_for_profile`` is the
second, since it's shared with the (trusted) map UI form and sanitizes
regardless of caller.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.push_device import PushTransport


class WhoAmISerializer(serializers.Serializer):
    """The only profile data an external application may ever read: its owner's uuid."""

    uuid = serializers.UUIDField(read_only=True)


class PinCreateSerializer(serializers.Serializer):
    """Validates an untrusted pin-creation payload from an external application.

    A conservative subset of what the map UI's "Add pin" form accepts (see
    ``controllers.maps.MapController.post_add_pin``) - label/tag/category ids,
    custom icon uploads, and Google Place linking are internal-only concepts
    and not exposed here.
    """

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    latitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-180, max_value=180)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True, default=None)
    icon = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    color = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True, default=None)
    #: Caller-generated idempotency uuid - an offline client stamps its pin at
    #: capture time and retries the same submission until acknowledged; a
    #: repeat is answered with the already-created pin instead of a duplicate.
    uuid = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate(self, attrs: dict) -> dict:
        """Require either coordinates or an address - mirrors the map form's own client-side check."""
        has_coords = attrs.get("latitude") is not None and attrs.get("longitude") is not None
        has_address = bool((attrs.get("address") or "").strip())
        if not has_coords and not has_address:
            raise serializers.ValidationError("Provide either latitude/longitude or an address.")
        return attrs


class PinSyncQuerySerializer(serializers.Serializer):
    """Validates the query params of the pin delta-sync endpoint."""

    modified_since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    cursor = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    limit = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    include_total = serializers.BooleanField(required=False, default=False)


class TombstoneSyncQuerySerializer(serializers.Serializer):
    """Validates the query params of the pin-deletions delta-sync endpoint."""

    deleted_since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    cursor = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    limit = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)


class SyncPinSerializer(serializers.Serializer):
    """Documents the pin payload shape served by the delta-sync endpoint.

    Schema-only: the actual payload is built by
    ``services.pin_sync._serialize_sync_pin`` (the map payload plus sync-only
    fields), never by this class - but the OpenAPI contract (and the Dart
    client generated from it) needs the shape spelled out.
    ``test_external_api_schema`` asserts these fields exactly match what the
    service really emits, so the two cannot silently drift.
    """

    id = serializers.IntegerField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    icon = serializers.CharField(read_only=True, allow_null=True)
    description = serializers.CharField(read_only=True, allow_blank=True)
    priority = serializers.IntegerField(read_only=True, allow_null=True)
    #: ISO datetime of the last visit, or the literal string "never".
    last_visited = serializers.CharField(read_only=True)
    latitude = serializers.FloatField(read_only=True)
    longitude = serializers.FloatField(read_only=True)
    status = serializers.CharField(read_only=True, allow_blank=True)
    categories = serializers.ListField(read_only=True, child=serializers.CharField())
    profile = serializers.IntegerField(read_only=True)
    rating = serializers.IntegerField(read_only=True)
    color = serializers.CharField(read_only=True, allow_null=True)
    tags = serializers.ListField(read_only=True, child=serializers.DictField())
    address = serializers.CharField(read_only=True, allow_blank=True, allow_null=True)
    own_icon = serializers.CharField(read_only=True, allow_null=True)
    own_custom_icon_url = serializers.CharField(read_only=True, allow_null=True)
    own_color = serializers.CharField(read_only=True, allow_null=True)
    child_count = serializers.IntegerField(read_only=True)
    pin_type = serializers.CharField(read_only=True)
    parent_uuid = serializers.UUIDField(read_only=True, allow_null=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class PinSyncResponseSerializer(serializers.Serializer):
    """Documents the envelope of the pin delta-sync endpoint (schema-only)."""

    pins = SyncPinSerializer(many=True, read_only=True)
    next_cursor = serializers.CharField(read_only=True, allow_null=True)
    sync_watermark = serializers.DateTimeField(read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)


class TombstoneSerializer(serializers.Serializer):
    """Documents one pin deletion in the deleted feed (schema-only)."""

    pin_uuid = serializers.UUIDField(read_only=True)
    deleted_at = serializers.DateTimeField(read_only=True)


class TombstoneSyncResponseSerializer(serializers.Serializer):
    """Documents the envelope of the pin-deletions endpoint (schema-only)."""

    tombstones = TombstoneSerializer(many=True, read_only=True)
    next_cursor = serializers.CharField(read_only=True, allow_null=True)
    sync_watermark = serializers.DateTimeField(read_only=True)


class PinCreateResponseSerializer(serializers.Serializer):
    """Documents the pin-create response (schema-only)."""

    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    name = serializers.CharField(read_only=True)
    ambiguous_location = serializers.BooleanField(read_only=True)
    created = serializers.BooleanField(read_only=True)


class ErrorSerializer(serializers.Serializer):
    """Documents the error envelope every external endpoint uses (schema-only)."""

    error = serializers.CharField(read_only=True)


class PushDeviceRegisterSerializer(serializers.Serializer):
    """Validates a native client's push-destination registration."""

    transport = serializers.ChoiceField(choices=PushTransport.choices, default=PushTransport.UNIFIEDPUSH)
    address = serializers.CharField(max_length=500)
    name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")


class PushDeviceResponseSerializer(serializers.Serializer):
    """The registered device as echoed back to the client.

    Deliberately excludes ``address``: a UnifiedPush endpoint URL is a
    send-capability secret, and the caller already knows what it submitted.
    """

    uuid = serializers.UUIDField(read_only=True)
    transport = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
