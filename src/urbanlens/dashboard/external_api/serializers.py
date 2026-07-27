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

import math

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status as NotificationStatus
from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.pin_suggestions.model import MAX_SUGGESTION_ALIASES, MAX_SUGGESTION_LINKS, MAX_SUGGESTION_PHOTOS
from urbanlens.dashboard.models.profile.meta import (
    DistanceUnit,
    GuidanceLevel,
    MapCenterMode,
    MapViewChoice,
    SyncAliasesDirection,
    ThemeChoice,
    VisibilityChoice,
)
from urbanlens.dashboard.models.profile.model import _COMMUNITY_GATED_VISIBILITY_FIELDS
from urbanlens.dashboard.models.push_device import PushTransport
from urbanlens.dashboard.services.map_pins.payload import MapPinPayloadService
from urbanlens.dashboard.services.notification_center import preference_field_names
from urbanlens.dashboard.services.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH, MAX_PROFILE_BIO_LENGTH

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
    limit = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1, max_value=MapPinPayloadService.MAX_LIMIT)
    include_total = serializers.BooleanField(required=False, default=False)


class TombstoneSyncQuerySerializer(serializers.Serializer):
    """Validates the query params of the pin-deletions delta-sync endpoint."""

    deleted_since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    cursor = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    limit = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1, max_value=MapPinPayloadService.MAX_LIMIT)


class SyncPinSerializer(serializers.Serializer):
    """Documents the pin payload shape served by the delta-sync endpoint.

    Schema-only: the actual payload is built by
    ``services.pin_sync.serialize_sync_pin`` (the map payload plus sync-only
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


class PinNoteDetailSerializer(serializers.Serializer):
    """One personal note, as nested in a pin-detail response (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    text = serializers.CharField(read_only=True)
    created = serializers.DateTimeField(read_only=True)


class PinAliasDetailSerializer(serializers.Serializer):
    """One alternate name, as nested in a pin-detail response (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)


class PinLinkDetailSerializer(serializers.Serializer):
    """One external link, as nested in a pin-detail response (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    url = serializers.CharField(read_only=True)
    wayback_url = serializers.CharField(read_only=True, allow_null=True)


class PinCustomFieldDetailSerializer(serializers.Serializer):
    """One custom field's value on this pin (schema-only).

    ``value`` is deliberately untyped: its shape follows ``type`` (text,
    number, date, time, a boolean checkbox, or a reference object) exactly
    as ``CustomFieldValue.export_value()`` returns it.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    type = serializers.CharField(read_only=True)
    value = serializers.JSONField(read_only=True, allow_null=True)


class PinSecurityDetailSerializer(serializers.Serializer):
    """The 8 security-indicator fields shared by Pin and Wiki (schema-only)."""

    fences = serializers.CharField(read_only=True)
    alarms = serializers.CharField(read_only=True)
    cameras = serializers.CharField(read_only=True)
    security = serializers.CharField(read_only=True)
    signs = serializers.CharField(read_only=True)
    vps = serializers.CharField(read_only=True)
    plywood = serializers.CharField(read_only=True)
    locked = serializers.CharField(read_only=True)


class PinDetailSerializer(SyncPinSerializer):
    """Documents the full pin-detail response (schema-only).

    A superset of :class:`SyncPinSerializer` - see
    ``services.pin_detail.build_pin_detail``, the function that actually
    builds this payload. ``test_external_api_schema.PinDetailContractTests``
    asserts these fields exactly match what that function really emits.
    """

    official_name = serializers.CharField(read_only=True, allow_null=True)
    date_built = serializers.DateField(read_only=True, allow_null=True)
    date_abandoned = serializers.DateField(read_only=True, allow_null=True)
    date_last_active = serializers.DateField(read_only=True, allow_null=True)
    security = PinSecurityDetailSerializer(read_only=True)
    wiki_slug = serializers.CharField(read_only=True, allow_null=True)
    cover_photo_url = serializers.CharField(read_only=True, allow_null=True)
    boundary = serializers.JSONField(read_only=True, allow_null=True)
    notes = PinNoteDetailSerializer(many=True, read_only=True)
    aliases = PinAliasDetailSerializer(many=True, read_only=True)
    links = PinLinkDetailSerializer(many=True, read_only=True)
    custom_fields = PinCustomFieldDetailSerializer(many=True, read_only=True)
    note_count = serializers.IntegerField(read_only=True)
    alias_count = serializers.IntegerField(read_only=True)
    link_count = serializers.IntegerField(read_only=True)


class PinUpdateSerializer(serializers.Serializer):
    """Validates an untrusted pin-update payload.

    Mirrors what the internal ``PinViewSet``/map-drag flow can already do
    (rename, re-icon, move, log a visit date) plus one addition the mobile
    app needs that the internal surface has no single endpoint for:
    ``parent_id``, to detach a pin (``null``) or re-parent it under another
    of the caller's own pins (its uuid). Coordinates, when present, must
    both be present and non-null - a pin can't be moved to "half a point".
    """

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    icon = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    last_visited = serializers.DateTimeField(required=False, allow_null=True)
    latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    #: A pin uuid to become this pin's new parent, or null to detach it to a
    #: top-level pin of its own. Omit entirely to leave the parent untouched.
    parent_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs: dict) -> dict:
        """Coordinates move together or not at all, and must be finite."""
        has_lat = "latitude" in attrs
        has_lng = "longitude" in attrs
        if has_lat != has_lng:
            raise serializers.ValidationError("Provide both latitude and longitude together.")
        if has_lat and (attrs["latitude"] is None or attrs["longitude"] is None):
            raise serializers.ValidationError("latitude and longitude cannot be null.")
        if has_lat and not (math.isfinite(attrs["latitude"]) and math.isfinite(attrs["longitude"])):
            raise serializers.ValidationError("latitude and longitude must be finite numbers.")
        return attrs


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


class SettingsFeaturesSerializer(serializers.Serializer):
    """Which feature-gated settings groups the caller's account can use (schema-only).

    A client uses this to hide the AI and Places cards outright rather than
    offering fields that a PATCH would reject.
    """

    ai = serializers.BooleanField(read_only=True)
    places = serializers.BooleanField(read_only=True)


class SettingsSerializer(serializers.Serializer):
    """The full account-preferences document served by ``GET settings/``.

    Every field is spelled out by hand rather than generated from ``Profile``
    by a ``ModelSerializer``. That is the whole point: ``Profile`` also carries
    location history, onboarding state, subscription linkage and other things
    an external client has no business reading, and a model-derived serializer
    would leak each new such field the moment someone added it. An explicit
    list fails closed - a new preference is invisible here until deliberately
    added to ``services.profile_settings.SETTINGS_FIELDS`` and to this class.

    Fields mirror that allowlist exactly; the trailing read-only keys are
    computed context (see ``services.profile_settings.read_settings``).
    """

    # Privacy visibilities.
    profile_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    comment_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    friend_request_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    photo_upload_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    viewer_photo_filter = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    trip_pin_location_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    contact_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    direct_message_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    common_pins_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    # Direct messages.
    online_status_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    read_receipt_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    typing_indicator_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, read_only=True)
    direct_message_delete_after = serializers.ChoiceField(choices=MessageRetentionChoice.choices, read_only=True)
    allow_friend_recommendations = serializers.BooleanField(read_only=True)
    # Style.
    theme_mode = serializers.ChoiceField(choices=ThemeChoice.choices, read_only=True)
    map_dark_mode = serializers.ChoiceField(choices=ThemeChoice.choices, read_only=True)
    guidance_level = serializers.ChoiceField(choices=GuidanceLevel.choices, read_only=True)
    distance_units = serializers.ChoiceField(choices=DistanceUnit.choices, read_only=True, allow_null=True)
    # Map display.
    default_map_view = serializers.ChoiceField(choices=MapViewChoice.choices, read_only=True)
    cluster_radius = serializers.IntegerField(read_only=True, allow_null=True)
    use_pin_cache = serializers.BooleanField(read_only=True)
    suggest_pin_restructure = serializers.BooleanField(read_only=True)
    # Map center.
    map_center_mode = serializers.ChoiceField(choices=MapCenterMode.choices, read_only=True)
    map_custom_latitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True, allow_null=True)
    map_custom_longitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True, allow_null=True)
    map_default_zoom = serializers.IntegerField(read_only=True)
    # Markup defaults.
    markup_fill_color = serializers.CharField(max_length=20, read_only=True)
    markup_fill_opacity = serializers.IntegerField(read_only=True)
    markup_border_color = serializers.CharField(max_length=20, read_only=True, allow_blank=True)
    markup_border_opacity = serializers.IntegerField(read_only=True)
    # Places layer (feature-gated).
    places_google_enabled = serializers.BooleanField(read_only=True)
    places_nps_enabled = serializers.BooleanField(read_only=True)
    places_wikipedia_enabled = serializers.BooleanField(read_only=True)
    # AI (feature-gated).
    ai_enabled = serializers.BooleanField(read_only=True)
    ai_label_categories = serializers.BooleanField(read_only=True)
    ai_label_tags = serializers.BooleanField(read_only=True)
    ai_label_statuses = serializers.BooleanField(read_only=True)
    # Keyword tagging.
    keyword_tagging_enabled = serializers.BooleanField(read_only=True)
    keyword_label_categories = serializers.BooleanField(read_only=True)
    keyword_label_tags = serializers.BooleanField(read_only=True)
    keyword_label_statuses = serializers.BooleanField(read_only=True)
    # History.
    track_pin_visits = serializers.BooleanField(read_only=True)
    track_routes = serializers.BooleanField(read_only=True)
    track_geolocation = serializers.BooleanField(read_only=True)
    generate_photo_keywords = serializers.BooleanField(read_only=True)
    # Community.
    community_enabled = serializers.BooleanField(read_only=True)
    show_wiki_cover_photos = serializers.BooleanField(read_only=True)
    auto_create_pin_article_from_wikipedia = serializers.BooleanField(read_only=True)
    # Pin suggestions.
    pin_suggestions_enabled = serializers.BooleanField(read_only=True)
    suggest_public_pins = serializers.BooleanField(read_only=True)
    suggest_pins_from_photos = serializers.BooleanField(read_only=True)
    suggest_pins_from_external_apis = serializers.BooleanField(read_only=True)
    # Wiki sync.
    sync_rating_to_wiki = serializers.BooleanField(read_only=True)
    sync_vulnerability_to_wiki = serializers.BooleanField(read_only=True)
    sync_priority_to_wiki = serializers.BooleanField(read_only=True)
    sync_danger_to_wiki = serializers.BooleanField(read_only=True)
    sync_aliases = serializers.ChoiceField(choices=SyncAliasesDirection.choices, read_only=True)
    # External APIs.
    external_apis_enabled = serializers.BooleanField(read_only=True)
    # Storage downscaling.
    image_downscale_max_dimension = serializers.IntegerField(read_only=True, allow_null=True)
    video_downscale_max_height = serializers.IntegerField(read_only=True, allow_null=True)
    # Read-only computed context.
    updated = serializers.DateTimeField(read_only=True)
    #: The unit actually in effect, with the profile's location-inferred
    #: fallback applied - ``distance_units`` itself may be null.
    effective_distance_units = serializers.CharField(read_only=True)
    features = SettingsFeaturesSerializer(read_only=True)
    allowed_image_dimensions = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    allowed_video_heights = serializers.ListField(child=serializers.IntegerField(), read_only=True)


class SettingsPatchSerializer(serializers.Serializer):
    """Validates a partial account-preferences update from ``PATCH settings/``.

    Every field is ``required=False`` **and carries no default**, deliberately:
    presence in ``validated_data`` is what distinguishes "the client did not
    submit this field" from "the client set it to null/false". A default would
    collapse those two cases and make every PATCH a full overwrite, so a client
    syncing one toggle would silently reset everything else.

    Mirrors :class:`SettingsSerializer` field for field, minus the computed
    read-only keys.
    """

    # Privacy visibilities.
    profile_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    comment_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    friend_request_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    photo_upload_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    viewer_photo_filter = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    trip_pin_location_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    contact_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    direct_message_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    common_pins_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    # Direct messages.
    online_status_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    read_receipt_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    typing_indicator_visibility = serializers.ChoiceField(choices=VisibilityChoice.choices, required=False)
    direct_message_delete_after = serializers.ChoiceField(choices=MessageRetentionChoice.choices, required=False)
    allow_friend_recommendations = serializers.BooleanField(required=False)
    # Style.
    theme_mode = serializers.ChoiceField(choices=ThemeChoice.choices, required=False)
    map_dark_mode = serializers.ChoiceField(choices=ThemeChoice.choices, required=False)
    guidance_level = serializers.ChoiceField(choices=GuidanceLevel.choices, required=False)
    #: Null resets to "infer from my location" - see Profile.effective_distance_units.
    distance_units = serializers.ChoiceField(choices=DistanceUnit.choices, required=False, allow_null=True)
    # Map display.
    default_map_view = serializers.ChoiceField(choices=MapViewChoice.choices, required=False)
    cluster_radius = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=1000)
    use_pin_cache = serializers.BooleanField(required=False)
    suggest_pin_restructure = serializers.BooleanField(required=False)
    # Map center.
    map_center_mode = serializers.ChoiceField(choices=MapCenterMode.choices, required=False)
    map_custom_latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True, min_value=-90, max_value=90)
    map_custom_longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True, min_value=-180, max_value=180)
    map_default_zoom = serializers.IntegerField(required=False, min_value=0, max_value=22)
    # Markup defaults.
    markup_fill_color = serializers.CharField(max_length=20, required=False)
    markup_fill_opacity = serializers.IntegerField(required=False, min_value=0, max_value=100)
    markup_border_color = serializers.CharField(max_length=20, required=False, allow_blank=True)
    markup_border_opacity = serializers.IntegerField(required=False, min_value=0, max_value=100)
    # Places layer (feature-gated - rejected with 400 while the feature is off).
    places_google_enabled = serializers.BooleanField(required=False)
    places_nps_enabled = serializers.BooleanField(required=False)
    places_wikipedia_enabled = serializers.BooleanField(required=False)
    # AI (feature-gated - rejected with 400 while the feature is off).
    ai_enabled = serializers.BooleanField(required=False)
    ai_label_categories = serializers.BooleanField(required=False)
    ai_label_tags = serializers.BooleanField(required=False)
    ai_label_statuses = serializers.BooleanField(required=False)
    # Keyword tagging.
    keyword_tagging_enabled = serializers.BooleanField(required=False)
    keyword_label_categories = serializers.BooleanField(required=False)
    keyword_label_tags = serializers.BooleanField(required=False)
    keyword_label_statuses = serializers.BooleanField(required=False)
    # History.
    track_pin_visits = serializers.BooleanField(required=False)
    track_routes = serializers.BooleanField(required=False)
    track_geolocation = serializers.BooleanField(required=False)
    generate_photo_keywords = serializers.BooleanField(required=False)
    # Community. Turning community_enabled off coerces the visibility and
    # wiki-sync fields in Profile.save(); the response reports the result.
    community_enabled = serializers.BooleanField(required=False)
    show_wiki_cover_photos = serializers.BooleanField(required=False)
    auto_create_pin_article_from_wikipedia = serializers.BooleanField(required=False)
    # Pin suggestions.
    pin_suggestions_enabled = serializers.BooleanField(required=False)
    suggest_public_pins = serializers.BooleanField(required=False)
    suggest_pins_from_photos = serializers.BooleanField(required=False)
    suggest_pins_from_external_apis = serializers.BooleanField(required=False)
    # Wiki sync.
    sync_rating_to_wiki = serializers.BooleanField(required=False)
    sync_vulnerability_to_wiki = serializers.BooleanField(required=False)
    sync_priority_to_wiki = serializers.BooleanField(required=False)
    sync_danger_to_wiki = serializers.BooleanField(required=False)
    sync_aliases = serializers.ChoiceField(choices=SyncAliasesDirection.choices, required=False)
    # External APIs.
    external_apis_enabled = serializers.BooleanField(required=False)
    #: Null means "no downscaling preference"; any value is checked against the
    #: caller's plan entitlement in services.profile_settings.
    image_downscale_max_dimension = serializers.IntegerField(required=False, allow_null=True)
    video_downscale_max_height = serializers.IntegerField(required=False, allow_null=True)


class AuthSessionSerializer(serializers.Serializer):
    """Describes the credential the request authenticated with (schema-only).

    Lets a client answer "what am I actually allowed to do?" without probing
    endpoints and collecting 403s - it can hide unreachable UI up front, and
    schedule a refresh before ``expires_at``.
    """

    #: Either "api_key" (PAT-style) or "oauth2" (an access token).
    credential_type = serializers.CharField(read_only=True)
    scopes = serializers.ListField(child=serializers.CharField(), read_only=True)
    #: Null for API keys, which do not expire - they are revoked instead.
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    issued_at = serializers.DateTimeField(read_only=True, allow_null=True)
    #: Null for API keys, which belong to no registered OAuth2 client.
    client_id = serializers.CharField(read_only=True, allow_null=True)
    #: The key's user-facing label, or the OAuth2 application's display name.
    name = serializers.CharField(read_only=True, allow_null=True)
    user_uuid = serializers.UUIDField(read_only=True)


class FriendProfileSerializer(serializers.Serializer):
    """A person as they may be shown to the caller, masking included.

    Never populated straight off a ``Profile``. Callers must build the dict
    through ``services.identity_visibility.resolve_visible_identity`` so a
    profile whose privacy settings don't permit the caller is masked here
    exactly as it is in the web UI - the API surface must not become the way
    to read a name the site itself would hide.
    """

    uuid = serializers.UUIDField(read_only=True)
    username = serializers.CharField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    avatar_url = serializers.CharField(read_only=True, allow_null=True)
    #: True when the fields above are placeholders rather than real identity.
    is_masked = serializers.BooleanField(read_only=True)


class FriendshipSerializer(serializers.Serializer):
    """One friend relationship from the calling profile's point of view.

    ``status`` and ``relationship_type`` are sourced from
    ``FriendshipStatus``/``FriendshipType`` directly, so the wire values are
    the model's own capitalized strings ("Accepted", "Requested", ...). They
    are deliberately *not* normalized to lowercase: every other enum on this
    surface happens to be lowercase snake_case, and assuming this one matched
    has already caused one real bug.
    """

    profile = FriendProfileSerializer(read_only=True)
    status = serializers.ChoiceField(choices=FriendshipStatus.choices, read_only=True)
    relationship_type = serializers.ChoiceField(choices=FriendshipType.choices, read_only=True)
    #: Which way the original request ran, relative to the caller.
    direction = serializers.ChoiceField(choices=[("incoming", "Incoming"), ("outgoing", "Outgoing")], read_only=True)
    message = serializers.CharField(read_only=True, allow_null=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class FriendListQuerySerializer(serializers.Serializer):
    """Validates the friends-list query string."""

    status = serializers.ChoiceField(choices=FriendshipStatus.choices, required=False)
    cursor = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)


class FriendListResponseSerializer(serializers.Serializer):
    """One page of the caller's friend relationships (schema-only)."""

    results = FriendshipSerializer(many=True, read_only=True)
    next_cursor = serializers.CharField(read_only=True, allow_null=True)


class FriendRequestCreateSerializer(serializers.Serializer):
    """Validates a friend request aimed at a profile the caller names by uuid."""

    profile_uuid = serializers.UUIDField()
    message = serializers.CharField(max_length=MAX_FRIEND_REQUEST_MESSAGE_LENGTH, required=False, allow_blank=True)


class FriendInviteSerializer(serializers.Serializer):
    """Validates an invite-by-email submission.

    Deliberately has no ``subscription_role`` field. The web form accepts one
    (site admins can attach a subscription grant to an invitation), but that
    is a privilege-escalation path with no business on an API-key surface -
    omitting it here means a key can never reach it, regardless of what the
    key owner's account could do while logged in.
    """

    email = serializers.EmailField()
    message = serializers.CharField(max_length=MAX_FRIEND_REQUEST_MESSAGE_LENGTH, required=False, allow_blank=True)


class FriendInviteResponseSerializer(serializers.Serializer):
    """The invite endpoint's single, invariant response (schema-only).

    ``result`` is always the literal ``"sent"``. It does not vary by whether
    the address was registered, whether the target's privacy settings
    accepted the request, or whether the mail actually went out - see
    ``services.friendship.invite_by_email``. Anything that made this field (or
    the status code, or the headers) branch would hand a caller an
    account-enumeration oracle.
    """

    result = serializers.CharField(read_only=True)


def _visibility_fields() -> dict[str, serializers.Field]:
    """Build one ``ChoiceField`` per community-gated visibility setting.

    Generated from ``_COMMUNITY_GATED_VISIBILITY_FIELDS`` rather than typed
    out, so a thirteenth visibility setting added to ``Profile`` appears on
    this surface automatically instead of being silently omitted.

    Returns:
        Mapping of field name to an optional ``VisibilityChoice`` field.
    """
    return {name: serializers.ChoiceField(choices=VisibilityChoice.choices, required=False) for name in _COMMUNITY_GATED_VISIBILITY_FIELDS}


ProfileVisibilitySerializer = type(
    "ProfileVisibilitySerializer",
    (serializers.Serializer,),
    {
        "__doc__": (
            "The caller's own per-field visibility settings.\n\n"
            "    Served only when viewing your own profile - another user's privacy\n"
            "    configuration is itself private. Fields are generated from\n"
            "    ``Profile._COMMUNITY_GATED_VISIBILITY_FIELDS`` (12 of them at the time\n"
            "    of writing) so the two cannot drift.\n    "
        ),
        **_visibility_fields(),
    },
)


class ProfileContactSerializer(serializers.Serializer):
    """A profile's contact methods, served only when contact visibility permits.

    Gated by ``Profile.can_view_contact_info``, which (unlike most visibility
    settings) does not treat a merely-pending friend request as sufficient.
    """

    phone_number = serializers.CharField(read_only=True, allow_blank=True)
    signal_username = serializers.CharField(read_only=True, allow_blank=True)
    discord_username = serializers.CharField(read_only=True, allow_blank=True)
    whatsapp_number = serializers.CharField(read_only=True, allow_blank=True)
    telegram_username = serializers.CharField(read_only=True, allow_blank=True)
    matrix_handle = serializers.CharField(read_only=True, allow_blank=True)


class ProfileDetailSerializer(serializers.Serializer):
    """A profile as the caller is permitted to see it."""

    uuid = serializers.UUIDField(read_only=True)
    username = serializers.CharField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    avatar_url = serializers.CharField(read_only=True, allow_null=True)
    bio = serializers.CharField(read_only=True, allow_null=True)
    area = serializers.CharField(read_only=True, allow_null=True)
    started_exploring = serializers.DateField(read_only=True, allow_null=True)
    is_self = serializers.BooleanField(read_only=True)
    #: Null when no relationship row exists at all.
    friendship_status = serializers.ChoiceField(choices=FriendshipStatus.choices, read_only=True, allow_null=True)
    #: Omitted unless contact visibility permits this caller.
    contact = ProfileContactSerializer(read_only=True, allow_null=True)
    #: Present only on your own profile.
    visibility = ProfileVisibilitySerializer(read_only=True, allow_null=True)


ProfileUpdateSerializer = type(
    "ProfileUpdateSerializer",
    (serializers.Serializer,),
    {
        "__doc__": (
            "Validates a partial update to the caller's own profile.\n\n"
            "    Excludes ``avatar`` on purpose: image upload is the Photos domain's\n"
            "    problem (size limits, downscaling, quota) and wiring a second upload\n"
            "    path through here would duplicate all of it. ``avatar_url`` stays\n"
            "    read-only until that service is reused here.\n\n"
            "    Community-gated visibility fields may be submitted, but\n"
            "    ``Profile.save()`` still coerces them to NO_ONE when community is\n"
            "    off - that enforcement is not reimplemented here.\n    "
        ),
        "bio": serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=MAX_PROFILE_BIO_LENGTH),
        "area": serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255),
        "started_exploring": serializers.DateField(required=False, allow_null=True),
        "distance_units": serializers.ChoiceField(choices=DistanceUnit.choices, required=False, allow_null=True),
        "theme_mode": serializers.ChoiceField(choices=ThemeChoice.choices, required=False),
        "community_enabled": serializers.BooleanField(required=False),
        **_visibility_fields(),
    },
)


class ProfileNoteSerializer(serializers.Serializer):
    """A private note the caller keeps about another profile."""

    uuid = serializers.UUIDField(read_only=True)
    content = serializers.CharField(read_only=True, allow_blank=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class ProfileNoteWriteSerializer(serializers.Serializer):
    """Validates a profile-note create or update."""

    content = serializers.CharField(allow_blank=True, max_length=MAX_PROFILE_BIO_LENGTH)


class NotificationSerializer(serializers.Serializer):
    """One notification from the caller's own inbox.

    Every enum here is lowercase snake_case, matching its model definition -
    unlike ``FriendshipSerializer.status`` above, which is capitalized. The
    difference is real; do not normalize either to match the other.
    """

    uuid = serializers.UUIDField(read_only=True)
    notification_type = serializers.ChoiceField(choices=NotificationType.choices, read_only=True)
    status = serializers.ChoiceField(choices=NotificationStatus.choices, read_only=True)
    importance = serializers.ChoiceField(choices=Importance.choices, read_only=True)
    title = serializers.CharField(read_only=True, allow_blank=True)
    message = serializers.CharField(read_only=True, allow_blank=True)
    url = serializers.CharField(read_only=True, allow_blank=True)
    created = serializers.DateTimeField(read_only=True)
    #: Null for notifications no person triggered (system/safety/errors).
    source_profile = FriendProfileSerializer(read_only=True, allow_null=True)


class NotificationListQuerySerializer(serializers.Serializer):
    """Validates the notifications-list query string."""

    unread_only = serializers.BooleanField(required=False, default=False)
    cursor = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)


class NotificationListResponseSerializer(serializers.Serializer):
    """One page of the caller's notifications (schema-only)."""

    results = NotificationSerializer(many=True, read_only=True)
    next_cursor = serializers.CharField(read_only=True, allow_null=True)
    unread_count = serializers.IntegerField(read_only=True)


class UnreadCountSerializer(serializers.Serializer):
    """The caller's unread notification count (schema-only)."""

    unread_count = serializers.IntegerField(read_only=True)


class NotificationPreferenceEntrySerializer(serializers.Serializer):
    """Delivery settings for a single notification-preference stem.

    ``whatsapp`` and ``sms`` are separate booleans rather than members of
    ``DeliveryPreference`` because each is billed per message; both are forced
    off server-side when the profile has no number to deliver to.
    """

    delivery = serializers.ChoiceField(choices=DeliveryPreference.choices, required=False)
    whatsapp = serializers.BooleanField(required=False)
    sms = serializers.BooleanField(required=False)


def _preference_entry_fields() -> dict[str, serializers.Field]:
    """Build one nested entry field per real notification-preference stem.

    Driven by ``services.notification_center.preference_field_names``, which
    introspects the model - so a thirteenth preference becomes readable and
    writable here with no change to this module.

    Returns:
        Mapping of stem name to an optional nested entry serializer.
    """
    return {stem: NotificationPreferenceEntrySerializer(required=False) for stem in preference_field_names()}


NotificationPreferenceSerializer = type(
    "NotificationPreferenceSerializer",
    (serializers.Serializer,),
    {
        "__doc__": (
            "The caller's per-type notification delivery preferences.\n\n"
            "    Exposes exactly the stems ``NotificationPreference`` actually defines -\n"
            "    a strict subset of ``NotificationType``. Types with no preference column\n"
            "    are absent rather than defaulted, because inventing a value here would\n"
            "    imply a control that does not exist.\n    "
        ),
        **_preference_entry_fields(),
    },
)
