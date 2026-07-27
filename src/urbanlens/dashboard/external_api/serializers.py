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
from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH
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
from urbanlens.dashboard.models.push_device import PushTransport
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.map_pins.payload import MapPinPayloadService
from urbanlens.dashboard.services.text_limits import MAX_COMMENT_TEXT_LENGTH, MAX_TRIP_ACTIVITY_NOTES_LENGTH, MAX_TRIP_DESCRIPTION_LENGTH
from urbanlens.dashboard.services.trip_comments import ALLOWED_COMMENT_EMOJIS

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


# -- Trips ---------------------------------------------------------------------
#
# Every serializer below reads either a model instance or one of the plain dicts
# the shared trip services already build (``build_activity_rows``,
# ``build_comment_tree``) - DRF resolves a dotted ``source`` through Mappings and
# objects alike, so a render row serializes without a second shaping pass. That
# keeps the external payload derived from the exact same rows the internal panel
# renders, rather than from a parallel re-computation that could drift.


class TripMemberProfileSerializer(serializers.Serializer):
    """One person as they may be shown to the requesting viewer.

    Always sourced from ``services.identity_visibility.resolve_visible_identities``'
    masked output, never from the raw model fields. That matters for ``slug``
    in particular: a profile slug is derived from the username, so emitting it
    for someone whose privacy settings hide them from this viewer would undo
    the masking that ``display_name`` performs. It is null whenever the person
    is masked, and ``uuid`` - which discloses nothing - is the handle a client
    uses to address them on the member endpoints.
    """

    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.SerializerMethodField()
    display_name = serializers.CharField(read_only=True)
    avatar_url = serializers.CharField(source="display_avatar_url", read_only=True, allow_null=True)

    def get_slug(self, profile) -> str | None:
        """Return the profile slug, or None when this viewer sees a masked identity.

        Args:
            profile: A profile already resolved by ``resolve_visible_identities``.

        Returns:
            The slug, or None when masked.
        """
        return None if getattr(profile, "is_masked", False) else profile.slug


class TripSummarySerializer(serializers.Serializer):
    """One trip as it appears in a list (schema and response shape).

    The count fields come from ``TripQuerySet.for_list_page``'s annotations, so
    a list response costs the same queries the web list page already does. They
    are absent (and serialize as 0) on an un-annotated instance.
    """

    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True, allow_null=True)
    start_date = serializers.DateField(source="effective_start_date", read_only=True, allow_null=True)
    end_date = serializers.DateField(source="effective_end_date", read_only=True, allow_null=True)
    #: One of "planning", "upcoming", "active", "past".
    timeline_status = serializers.CharField(read_only=True)
    duration_days = serializers.IntegerField(read_only=True, allow_null=True)
    activity_count = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    pin_count = serializers.SerializerMethodField()
    is_creator = serializers.SerializerMethodField()
    #: "invited" or "joined"; null when the viewer is the creator with no row.
    membership_status = serializers.SerializerMethodField()
    rsvp = serializers.SerializerMethodField()
    is_organizer = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)

    def _viewer(self):
        """The requesting profile, supplied by the view through serializer context."""
        return self.context.get("viewer")

    def _membership(self, trip):
        """This viewer's membership row for *trip*, resolved at most once per trip."""
        cache = self.context.setdefault("_membership_cache", {})
        if trip.pk not in cache:
            from urbanlens.dashboard.models.trips.model import TripMembership

            viewer = self._viewer()
            cache[trip.pk] = TripMembership.objects.for_trip_and_profile(trip, viewer).first() if viewer else None
        return cache[trip.pk]

    def get_activity_count(self, trip) -> int:
        """Annotated activity count, or 0 on an un-annotated instance."""
        return getattr(trip, "activity_count", 0) or 0

    def get_member_count(self, trip) -> int:
        """Annotated member count, or 0 on an un-annotated instance."""
        return getattr(trip, "member_count", 0) or 0

    def get_comment_count(self, trip) -> int:
        """Annotated comment count, or 0 on an un-annotated instance."""
        return getattr(trip, "comment_count", 0) or 0

    def get_pin_count(self, trip) -> int:
        """Annotated count of activities linked to a pin, or 0 when un-annotated."""
        return getattr(trip, "pin_count", 0) or 0

    def get_is_creator(self, trip) -> bool:
        """Whether the requesting viewer created this trip."""
        viewer = self._viewer()
        return bool(viewer and trip.creator_id == viewer.id)

    def get_membership_status(self, trip) -> str | None:
        """The viewer's join status, or None when they have no membership row."""
        membership = self._membership(trip)
        return membership.status if membership else None

    def get_rsvp(self, trip) -> str | None:
        """The viewer's trip-wide RSVP, or None when unanswered."""
        membership = self._membership(trip)
        return membership.rsvp if membership else None

    def get_is_organizer(self, trip) -> bool:
        """Whether the viewer is the creator or a designated organizer."""
        viewer = self._viewer()
        if viewer is None:
            return False
        if trip.creator_id == viewer.id:
            return True
        membership = self._membership(trip)
        return bool(membership and membership.is_organizer)


class TripCalendarSyncStatusSerializer(serializers.Serializer):
    """Whether this trip is mirrored to the caller's Google Calendar (schema-only).

    ``connected`` and ``linked`` are separate deliberately: a client that finds
    ``connected`` false must send the user to the web app, because establishing
    a calendar connection needs an OAuth consent flow the external API does not
    (and should not) reproduce.
    """

    #: A GoogleCalendarAccount exists for the caller.
    connected = serializers.BooleanField(read_only=True)
    #: A trip-level export link exists for this trip and caller.
    linked = serializers.BooleanField(read_only=True)
    #: Whether later edits keep pushing to the linked event.
    auto_sync = serializers.BooleanField(read_only=True)
    last_synced = serializers.DateTimeField(read_only=True, allow_null=True)


class TripPermissionsSerializer(serializers.Serializer):
    """The trip's four configurable permission levels (schema and response shape)."""

    allow_add_members = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, read_only=True)
    allow_add_activities = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, read_only=True)
    allow_edit_activities = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, read_only=True)
    allow_comments = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, read_only=True)


class TripViewerSerializer(serializers.Serializer):
    """What the requesting caller specifically may see and do on this trip.

    The four ``can_*`` booleans are derived server-side from
    ``services.trip_access.can_perform``, not re-derived by the client from
    the permission levels - so a future change to how a level is evaluated
    reaches the app without an app release, and a client can gray out an
    action it would be refused rather than discovering that by 403.
    """

    has_joined = serializers.BooleanField(read_only=True)
    is_organizer = serializers.BooleanField(read_only=True)
    is_creator = serializers.BooleanField(read_only=True)
    membership_status = serializers.CharField(read_only=True, allow_null=True)
    rsvp = serializers.CharField(read_only=True, allow_null=True)
    can_add_members = serializers.BooleanField(read_only=True)
    can_add_activities = serializers.BooleanField(read_only=True)
    can_edit_activities = serializers.BooleanField(read_only=True)
    can_comment = serializers.BooleanField(read_only=True)


class TripMemberSerializer(serializers.Serializer):
    """One membership row: who, and where they stand on the trip."""

    profile = TripMemberProfileSerializer(read_only=True)
    status = serializers.ChoiceField(choices=TripMembership.STATUS_CHOICES, read_only=True)
    rsvp = serializers.ChoiceField(choices=TripMembership.RSVP_CHOICES, read_only=True, allow_null=True)
    is_organizer = serializers.BooleanField(read_only=True)
    is_creator = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)

    def get_is_creator(self, membership) -> bool:
        """Whether this member is the trip's creator."""
        return membership.profile_id == membership.trip.creator_id


class TripDetailSerializer(TripSummarySerializer):
    """One trip in full, including its roster.

    Members are bundled here rather than left to the paginated members
    endpoint: a trip's roster is small and bounded by ``max_trip_members``, and
    the app needs it to render the detail screen at all, so making it a second
    round trip would only add latency.
    """

    creator = TripMemberProfileSerializer(read_only=True, allow_null=True)
    permissions = TripPermissionsSerializer(source="*", read_only=True)
    viewer = TripViewerSerializer(read_only=True)
    calendar_sync = TripCalendarSyncStatusSerializer(read_only=True)
    members = TripMemberSerializer(many=True, read_only=True)


class TripActivityLegSerializer(serializers.Serializer):
    """The driving leg from the previous stop to this one (schema-only)."""

    distance_meters = serializers.FloatField(read_only=True)
    duration_seconds = serializers.FloatField(read_only=True)
    distance_display = serializers.CharField(read_only=True)
    duration_display = serializers.CharField(read_only=True)


class TripActivitySerializer(serializers.Serializer):
    """One itinerary entry, serialized from a ``build_activity_rows`` render row.

    ``latitude``/``longitude`` are **null** - not merely flagged - whenever the
    activity's location is effectively hidden from this viewer, whether by the
    activity's own ``location_hidden`` flag or by the adder's
    ``trip_pin_location_visibility`` privacy setting. A flag alone would leave
    the coordinates in the payload for any client that ignored it.
    """

    id = serializers.IntegerField(source="activity.id", read_only=True)
    title = serializers.CharField(source="activity.title", read_only=True, allow_null=True)
    #: The label the UI shows: the title, else the linked pin/location's name.
    effective_title = serializers.CharField(source="activity.effective_title", read_only=True)
    notes = serializers.CharField(source="activity.notes", read_only=True, allow_null=True)
    status = serializers.ChoiceField(choices=TripActivity.STATUS_CHOICES, source="activity.status", read_only=True)
    scheduled_at = serializers.DateTimeField(source="activity.scheduled_at", read_only=True, allow_null=True)
    scheduled_end = serializers.DateTimeField(source="activity.scheduled_end", read_only=True, allow_null=True)
    order = serializers.IntegerField(source="activity.order", read_only=True)
    #: The activity's 1-based map marker number, or null when it has no marker.
    index = serializers.IntegerField(read_only=True, allow_null=True)
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    #: The *effective* value - the activity's own flag OR this viewer's gate.
    location_hidden = serializers.BooleanField(source="effective_location_hidden", read_only=True)
    #: Present only when the linked pin belongs to the requesting caller.
    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    child_trip_uuid = serializers.SerializerMethodField()
    added_by = TripMemberProfileSerializer(source="activity.added_by", read_only=True, allow_null=True)
    vote_up = serializers.IntegerField(read_only=True)
    vote_down = serializers.IntegerField(read_only=True)
    user_vote = serializers.CharField(read_only=True, allow_null=True)
    #: The viewer's effective RSVP - their override if set, else the trip RSVP.
    rsvp = serializers.CharField(read_only=True, allow_null=True)
    rsvp_is_override = serializers.BooleanField(read_only=True)
    can_manage = serializers.BooleanField(read_only=True)
    has_coords = serializers.BooleanField(read_only=True)
    leg = TripActivityLegSerializer(read_only=True, allow_null=True)
    created = serializers.DateTimeField(source="activity.created", read_only=True)
    updated = serializers.DateTimeField(source="activity.updated", read_only=True)

    def _visible_coords(self, row) -> tuple[float, float] | None:
        """Coordinates this viewer may see, or None when hidden or absent."""
        from urbanlens.dashboard.services.trip_legs import activity_coords

        if row["effective_location_hidden"]:
            return None
        return activity_coords(row["activity"])

    def get_latitude(self, row) -> float | None:
        """Latitude, or None when the location is hidden from this viewer."""
        coords = self._visible_coords(row)
        return coords[0] if coords else None

    def get_longitude(self, row) -> float | None:
        """Longitude, or None when the location is hidden from this viewer."""
        coords = self._visible_coords(row)
        return coords[1] if coords else None

    def get_child_trip_uuid(self, row) -> str | None:
        """The uuid of the trip nested under this activity, if any."""
        child = row["activity"].child_trip
        return str(child.uuid) if child else None


class TripMapPointSerializer(serializers.Serializer):
    """Documents one trip-map marker (schema-only).

    The map endpoint returns ``services.trip_map.build_trip_map_points`` output
    verbatim so it stays byte-identical to the web map's own ``map-data/``
    payload; this class exists purely to describe that shape in the OpenAPI
    contract and is never used to serialize.
    """

    #: 1-based marker number, or null on a child trip's ghost marker.
    index = serializers.IntegerField(read_only=True, allow_null=True)
    #: Null on a ghost marker - it belongs to another trip's activity.
    activity_id = serializers.IntegerField(read_only=True, allow_null=True)
    label = serializers.CharField(read_only=True)
    lat = serializers.FloatField(read_only=True)
    lng = serializers.FloatField(read_only=True)
    status = serializers.ChoiceField(choices=TripActivity.STATUS_CHOICES, read_only=True)
    scheduled_at = serializers.DateTimeField(read_only=True, allow_null=True)
    draggable = serializers.BooleanField(read_only=True)
    #: Present (and True) only on a child trip's ghost markers.
    child_trip = serializers.BooleanField(read_only=True, required=False)


class TripMapResponseSerializer(serializers.Serializer):
    """Documents the trip-map envelope (schema-only)."""

    points = TripMapPointSerializer(many=True, read_only=True)


class TripCommentReactionSerializer(serializers.Serializer):
    """One emoji's tally on a comment, plus whether the caller is among them."""

    emoji = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True)
    reacted = serializers.BooleanField(read_only=True)


class TripCommentSerializer(serializers.Serializer):
    """One trip comment, serialized from a ``build_comment_tree`` row.

    Only comments this viewer is allowed to see are ever in the tree, so there
    is no visibility decision left to make here. Replies nest one level deep,
    which is all the data model produces in practice - a reply's own replies
    are not rendered by either surface.
    """

    id = serializers.IntegerField(source="comment.id", read_only=True)
    text = serializers.CharField(source="comment.text", read_only=True, allow_null=True)
    #: Mention-rendered, sanitized HTML - the same string the web panel shows.
    rendered_html = serializers.CharField(source="rendered_text", read_only=True)
    author = TripMemberProfileSerializer(source="comment.author", read_only=True, allow_null=True)
    image_url = serializers.SerializerMethodField()
    has_map = serializers.SerializerMethodField()
    created = serializers.DateTimeField(source="comment.created", read_only=True)
    can_delete = serializers.BooleanField(read_only=True)
    reactions = serializers.SerializerMethodField()
    replies = serializers.SerializerMethodField()

    def get_image_url(self, row) -> str | None:
        """The attached image's URL, or None when there isn't one."""
        image = row["comment"].image
        return image.url if image else None

    def get_has_map(self, row) -> bool:
        """Whether a markup map is attached to this comment."""
        return row["comment"].markup_map_id is not None

    def get_reactions(self, row) -> list[dict]:
        """Aggregate reactions with a per-caller ``reacted`` flag."""
        viewer = self.context.get("viewer")
        viewer_id = viewer.id if viewer else None
        return [
            {"emoji": emoji, "count": data["count"], "reacted": viewer_id in data["reacted_by"]}
            for emoji, data in sorted(row["reactions"].items())
        ]

    def get_replies(self, row) -> list[dict]:
        """Serialize this comment's visible replies (never nested further)."""
        return TripCommentSerializer(row.get("replies", []), many=True, context=self.context).data


# -- Trip request payloads -----------------------------------------------------


class TripCreateSerializer(serializers.Serializer):
    """Validates an untrusted trip-creation payload."""

    #: Optional - a blank submission gets a generated placeholder name, matching
    #: the web app's "just start planning" flow.
    name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    description = serializers.CharField(max_length=MAX_TRIP_DESCRIPTION_LENGTH, required=False, allow_blank=True, allow_null=True, default=None)
    start_date = serializers.DateField(required=False, allow_null=True, default=None)
    end_date = serializers.DateField(required=False, allow_null=True, default=None)
    #: Caller-generated idempotency uuid, mirroring PinCreateSerializer: an
    #: offline client retries the same submission until acknowledged, and a
    #: repeat is answered with the already-created trip instead of a duplicate.
    uuid = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate(self, attrs: dict) -> dict:
        """Reject a range that ends before it starts.

        Args:
            attrs: The validated field values.

        Returns:
            The unchanged values when the range is coherent.

        Raises:
            serializers.ValidationError: ``end_date`` precedes ``start_date``.
        """
        start, end = attrs.get("start_date"), attrs.get("end_date")
        if start and end and end < start:
            raise serializers.ValidationError("end_date cannot be before start_date.")
        return attrs


class TripUpdateSerializer(serializers.Serializer):
    """Validates a partial trip update.

    No field carries a default, so ``"x" in validated_data`` distinguishes
    "omitted" from "explicitly set to null" - the same presence-keyed pattern
    :class:`PinUpdateSerializer` uses, and what ``services.trip_crud.update_trip``
    expects.
    """

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(max_length=MAX_TRIP_DESCRIPTION_LENGTH, required=False, allow_blank=True, allow_null=True)
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)


class TripMemberAddSerializer(serializers.Serializer):
    """Validates an add-member submission."""

    #: Matched case-insensitively against Django's User.username.
    username = serializers.CharField(max_length=150, allow_blank=False)


class TripMemberOrganizerSerializer(serializers.Serializer):
    """Validates an organizer-flag change.

    Explicitly the target state rather than a toggle - a client retrying a
    request it never saw acknowledged would otherwise flip the flag back.
    """

    is_organizer = serializers.BooleanField()


class TripRsvpSerializer(serializers.Serializer):
    """Validates a trip-wide RSVP; null clears the answer."""

    rsvp = serializers.ChoiceField(choices=TripMembership.RSVP_CHOICES, allow_null=True)


class TripActivityCreateSerializer(serializers.Serializer):
    """Validates an untrusted activity-creation payload.

    Unlike the web form's split date and time inputs, schedule fields here are
    whole ISO datetimes - a native client has a real datetime to send, and
    splitting it only to recombine it server-side loses the timezone.
    """

    title = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    notes = serializers.CharField(max_length=MAX_TRIP_ACTIVITY_NOTES_LENGTH, required=False, allow_blank=True, allow_null=True, default=None)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    scheduled_end = serializers.DateTimeField(required=False, allow_null=True, default=None)
    #: One of the caller's own pins, by slug (or uuid) - never another user's.
    pin_slug = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    location_uuid = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True, default=None)
    latitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-180, max_value=180)
    #: Name to seed a Location created from raw coordinates.
    place_name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True, default=None)
    #: A trip of the caller's own to nest here; its stops appear as ghost markers.
    child_trip_uuid = serializers.UUIDField(required=False, allow_null=True, default=None)
    status = serializers.ChoiceField(choices=["proposed", "confirmed"], required=False, default="proposed")
    location_hidden = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict) -> dict:
        """Coordinates travel together, and a range must not end before it starts.

        Args:
            attrs: The validated field values.

        Returns:
            The unchanged values when coherent.

        Raises:
            serializers.ValidationError: Only one coordinate was supplied, or
                ``scheduled_end`` precedes ``scheduled_at``.
        """
        lat, lng = attrs.get("latitude"), attrs.get("longitude")
        if (lat is None) != (lng is None):
            raise serializers.ValidationError("Provide both latitude and longitude together.")
        start, end = attrs.get("scheduled_at"), attrs.get("scheduled_end")
        if start and end and end < start:
            raise serializers.ValidationError("scheduled_end cannot be before scheduled_at.")
        return attrs


class TripActivityUpdateSerializer(TripActivityCreateSerializer):
    """Validates a partial activity update.

    Every field drops its default so presence drives the update, exactly as in
    :class:`TripUpdateSerializer`.
    """

    title = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(max_length=MAX_TRIP_ACTIVITY_NOTES_LENGTH, required=False, allow_blank=True, allow_null=True)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    scheduled_end = serializers.DateTimeField(required=False, allow_null=True)
    pin_slug = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    location_uuid = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True)
    latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    place_name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    child_trip_uuid = serializers.UUIDField(required=False, allow_null=True)
    status = serializers.ChoiceField(choices=["proposed", "confirmed"], required=False)
    location_hidden = serializers.BooleanField(required=False)


class TripActivityPositionSerializer(serializers.Serializer):
    """Validates a map-drag position override.

    The bounds are the point of this serializer: the endpoint it replaces
    accepted any float and persisted it, so a marker could be saved at
    latitude 5000.
    """

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)


class TripActivityVoteSerializer(serializers.Serializer):
    """Validates a vote on a proposed activity; null clears it."""

    vote = serializers.ChoiceField(choices=["up", "down"], allow_null=True)


class TripActivityStatusSerializer(serializers.Serializer):
    """Validates an activity status change.

    ``completed`` is accepted here and routed to
    ``services.trip_activities.complete_activity``, which also logs the
    completer's visit and snaps a future date back to today - so a client never
    has to know that completing is a different operation from confirming.
    """

    status = serializers.ChoiceField(choices=["proposed", "confirmed", "completed"])


class TripActivityRsvpSerializer(serializers.Serializer):
    """Validates a per-activity RSVP override; null clears it."""

    rsvp = serializers.ChoiceField(choices=TripMembership.RSVP_CHOICES, allow_null=True)


class TripCommentCreateSerializer(serializers.Serializer):
    """Validates a trip comment submission.

    Image and map attachments are web-only for now: both need multipart upload
    and the markup-map editor's payload, neither of which this surface exposes.
    """

    text = serializers.CharField(max_length=MAX_COMMENT_TEXT_LENGTH, allow_blank=False)
    #: A comment on this same trip to reply to.
    parent_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class TripCommentReactionSetSerializer(serializers.Serializer):
    """Validates a reaction change; explicitly the target state, not a toggle."""

    emoji = serializers.ChoiceField(choices=sorted(ALLOWED_COMMENT_EMOJIS))
    reacted = serializers.BooleanField()


class TripCalendarSyncToggleSerializer(serializers.Serializer):
    """Validates a calendar auto-sync toggle for an already-exported trip."""

    enabled = serializers.BooleanField()


class TripListQuerySerializer(serializers.Serializer):
    """Validates the trips list's ordering query params."""

    sort = serializers.ChoiceField(choices=["start_date", "updated"], required=False, default="updated")
    dir = serializers.ChoiceField(choices=["asc", "desc"], required=False, default="desc")


class TripMapQuerySerializer(serializers.Serializer):
    """Validates the trip map's query params."""

    #: Completed stops are dropped unless asked for.
    include_past = serializers.BooleanField(required=False, default=False)


class TripActivityListQuerySerializer(serializers.Serializer):
    """Validates the activity list's query params."""

    #: Off by default - driving legs cost live routing calls, and a plain list
    #: fetch must never trigger them.
    include_legs = serializers.BooleanField(required=False, default=False)
