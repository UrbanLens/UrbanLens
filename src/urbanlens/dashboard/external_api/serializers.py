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

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH
from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.pin_suggestions.model import MAX_SUGGESTION_ALIASES, MAX_SUGGESTION_LINKS, MAX_SUGGESTION_PHOTOS
from urbanlens.dashboard.models.push_device import PushTransport

#: Same scheme restriction as controllers.links._clean_link_input - external
#: submissions are untrusted input, so this validates before anything else does.
_validate_link_url = URLValidator(schemes=["http", "https"])


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
    #: Personal notes captured in the field - same free-text field the pin
    #: detail page edits; bounded here because external input is untrusted.
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True, allow_null=True, default=None)
    #: What the marker physically represents. Omitted/null keeps the
    #: "location" default, leaving the pin eligible for automatic
    #: classification exactly like a map-UI drop.
    pin_type = serializers.ChoiceField(choices=PinType.choices, required=False, allow_null=True, default=None)
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


class LinkInputSerializer(serializers.Serializer):
    """One external link proposed for a pin suggestion."""

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    url = serializers.CharField(max_length=MAX_LINK_URL_LENGTH)

    def validate_url(self, value: str) -> str:
        """Restrict to http(s) - same rule ``controllers.links`` enforces for manually-added links."""
        try:
            _validate_link_url(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError("That doesn't look like a valid http(s) url.") from exc
        return value


class PinSuggestionCreateSerializer(serializers.Serializer):
    """Validates an untrusted pin-*suggestion* payload from an external application.

    Unlike ``PinCreateSerializer``, nothing here is written to a real Pin
    immediately - it's staged as a ``PinSuggestion`` the profile owner must
    explicitly accept before anything appears on their map (see
    ``services.pin_suggestions.ingest_location_hits``). This is why an
    external "discovery" app (finds candidate places autonomously, without
    the user having been there) should use this endpoint rather than
    ``PinCreateSerializer``/``PinsView.post``, which creates a real pin outright.
    """

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    latitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-180, max_value=180)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True, default=None)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True, allow_null=True, default=None)
    pin_type = serializers.ChoiceField(choices=PinType.choices, required=False, allow_null=True, default=None)
    #: Alternate names for the place - offered as PinAlias rows if accepted.
    aliases = serializers.ListField(child=serializers.CharField(max_length=255, allow_blank=False), required=False, default=list)
    #: External links about the place - offered as PinLink rows if accepted.
    links = LinkInputSerializer(many=True, required=False, default=list)
    #: Photo urls to download and stage as candidate gallery photos.
    photos = serializers.ListField(child=serializers.URLField(max_length=2048), required=False, default=list)

    def validate(self, attrs: dict) -> dict:
        """Require coordinates or an address, and enforce the same caps ``PinSuggestion`` stores."""
        has_coords = attrs.get("latitude") is not None and attrs.get("longitude") is not None
        has_address = bool((attrs.get("address") or "").strip())
        if not has_coords and not has_address:
            raise serializers.ValidationError("Provide either latitude/longitude or an address.")
        if len(attrs.get("aliases") or []) > MAX_SUGGESTION_ALIASES:
            raise serializers.ValidationError(f"Provide at most {MAX_SUGGESTION_ALIASES} aliases.")
        if len(attrs.get("links") or []) > MAX_SUGGESTION_LINKS:
            raise serializers.ValidationError(f"Provide at most {MAX_SUGGESTION_LINKS} links.")
        if len(attrs.get("photos") or []) > MAX_SUGGESTION_PHOTOS:
            raise serializers.ValidationError(f"Provide at most {MAX_SUGGESTION_PHOTOS} photos.")
        return attrs


class PinSuggestionCreateResponseSerializer(serializers.Serializer):
    """Documents the pin-suggestion-create response (schema-only)."""

    suggestion_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    matched_existing_pin = serializers.BooleanField(read_only=True)
    photos_attached = serializers.IntegerField(read_only=True)
    review_url = serializers.CharField(read_only=True)


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
