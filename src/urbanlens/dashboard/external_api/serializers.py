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
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.images.model import MediaKind
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
from urbanlens.dashboard.services.map_pins.payload import MapPinPayloadService
from urbanlens.dashboard.services.media_labels import MAX_MEDIA_LABEL_NAME_LENGTH, MAX_MEDIA_LABELS

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

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


class PhotoSerializer(serializers.Serializer):
    """One photo/video/document as an external client sees it (schema-only).

    Populated by :func:`build_photo_payload`, which resolves the
    viewer-dependent fields (``owner_slug``, ``wiki_*``, ``dm_peer_*``) - this
    class only declares the resulting shape.
    """

    uuid = serializers.UUIDField(read_only=True)
    media_type = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    #: Path under the authenticated media gate, not a public URL - fetching it
    #: needs the same credential plus the ``media:read`` scope.
    url = serializers.CharField(read_only=True, allow_null=True)
    caption = serializers.CharField(read_only=True, allow_null=True)
    author = serializers.CharField(read_only=True, allow_null=True)
    source_url = serializers.CharField(read_only=True, allow_null=True)
    copyright = serializers.CharField(read_only=True, allow_null=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True, allow_null=True)
    #: True when the coordinates came from the photo's shared Location rather
    #: than its own GPS - accurate to the place, not to the capture point.
    coordinates_are_estimated = serializers.BooleanField(read_only=True)
    direction = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True, allow_null=True)
    taken_at = serializers.DateTimeField(read_only=True, allow_null=True)
    created = serializers.DateTimeField(read_only=True)
    file_size = serializers.IntegerField(read_only=True, allow_null=True)
    labels = serializers.ListField(child=serializers.CharField(), read_only=True)
    organize_dismissed = serializers.BooleanField(read_only=True)
    #: Organize state, from ``services.memories.photos.classify_photo``.
    state = serializers.CharField(read_only=True)
    owner_slug = serializers.CharField(read_only=True, allow_null=True)
    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    pin_name = serializers.CharField(read_only=True, allow_null=True)
    visit_id = serializers.IntegerField(read_only=True, allow_null=True)
    wiki_slug = serializers.CharField(read_only=True, allow_null=True)
    wiki_name = serializers.CharField(read_only=True, allow_null=True)
    dm_peer_slug = serializers.CharField(read_only=True, allow_null=True)
    dm_peer_name = serializers.CharField(read_only=True, allow_null=True)


def build_photo_payload(image: Image, viewer_profile: Profile) -> dict:
    """Build one photo's external-API payload for a given viewer.

    Deliberately not ``services.images.image_to_gallery_json``: that one takes
    an ``HttpRequest``, builds absolute URLs and template-facing flags for the
    site's own gallery, and is free to change shape whenever the frontend
    needs it. This payload is a published contract.

    Every field naming a *person* or a *space the viewer may not belong to* is
    resolved through the same gate the site's own pages use, so a photo the
    viewer can legitimately see never becomes a side channel for context they
    cannot:

    - ``owner_slug`` goes through ``services.identity_visibility`` and is null
      when the uploader's privacy settings hide them from this viewer.
    - ``wiki_slug``/``wiki_name`` go through ``services.wiki_access`` and are
      null when the viewer has no standing to see that community page.
    - ``dm_peer_*`` is the other participant in the photo's originating direct
      message, and is null unless the viewer is one of the two participants -
      a photo can be visible through a pin gallery while the fact that it was
      also sent in someone's DM stays private.
    - Owner-only bookkeeping (``pin_*``, ``visit_id``, ``organize_dismissed``)
      is withheld for a photo the viewer merely has visibility on.

    Args:
        image: The photo to serialize. ``labels`` should be prefetched and
            ``pin``/``wiki``/``visit``/``location``/``profile`` selected, or
            this issues a query per field.
        viewer_profile: The profile the payload is being built for.

    Returns:
        A dict matching :class:`PhotoSerializer`.
    """
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity
    from urbanlens.dashboard.services.memories.photos import classify_photo
    from urbanlens.dashboard.services.wiki_access import location_visible_to

    is_owner = image.profile_id == viewer_profile.pk

    owner_slug = None
    if image.profile is not None and not resolve_visible_identity(viewer_profile, image.profile)["is_masked"]:
        owner_slug = image.profile.slug

    wiki_slug = wiki_name = None
    wiki = image.wiki
    if wiki is not None and wiki.location is not None and location_visible_to(wiki.location, viewer_profile):
        wiki_slug = wiki.slug
        wiki_name = wiki.name

    dm_peer_slug = dm_peer_name = None
    if image.direct_message_id:
        dm = image.direct_message
        if dm is not None and viewer_profile.pk in (dm.sender_id, dm.recipient_id):
            peer = dm.recipient if dm.sender_id == viewer_profile.pk else dm.sender
            if peer is not None:
                identity = resolve_visible_identity(viewer_profile, peer)
                if not identity["is_masked"]:
                    dm_peer_slug = peer.slug
                    dm_peer_name = identity["display_name"]

    return {
        "uuid": image.uuid,
        "media_type": image.media_type,
        "source": image.source,
        "url": image.image.url if image.image else None,
        "caption": image.caption,
        "author": image.author,
        "source_url": image.source_url,
        "copyright": image.copyright,
        "latitude": image.effective_latitude,
        "longitude": image.effective_longitude,
        "coordinates_are_estimated": image.latitude is None and image.effective_latitude is not None,
        "direction": image.direction,
        "taken_at": image.taken_at,
        "created": image.created,
        "file_size": image.file_size,
        "labels": [label.name for label in image.labels.all()],
        "organize_dismissed": image.organize_dismissed if is_owner else False,
        "state": classify_photo(image),
        "owner_slug": owner_slug,
        "pin_slug": image.pin.slug if (is_owner and image.pin is not None) else None,
        "pin_name": image.pin.effective_name if (is_owner and image.pin is not None) else None,
        "visit_id": image.visit_id if is_owner else None,
        "wiki_slug": wiki_slug,
        "wiki_name": wiki_name,
        "dm_peer_slug": dm_peer_slug,
        "dm_peer_name": dm_peer_name,
    }


class PhotoListQuerySerializer(serializers.Serializer):
    """Validates the filters on ``GET photos/``.

    Pagination itself is the standard ``page``/``page_size`` pair handled by
    ``external_api.pagination.ExternalApiPagination`` and is not declared here.
    """

    #: A pin slug or uuid; resolved against the caller's own pins only.
    pin = serializers.CharField(max_length=255, required=False, allow_blank=True)
    #: Restrict to photos filed to neither a pin nor a visit.
    unfiled = serializers.BooleanField(required=False, default=False)
    #: Bounds on capture time, falling back to upload time where unknown.
    taken_from = serializers.DateTimeField(required=False)
    taken_to = serializers.DateTimeField(required=False)
    media_type = serializers.ChoiceField(choices=MediaKind.choices, required=False)


class PhotoListResponseSerializer(serializers.Serializer):
    """The paginated photo list envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = PhotoSerializer(many=True, read_only=True)


class PhotoUploadSerializer(serializers.Serializer):
    """Validates a multipart photo upload from an external client."""

    file = serializers.FileField()
    caption = serializers.CharField(max_length=500, required=False, allow_blank=True)
    #: A pin slug or uuid; must be one of the caller's own pins.
    pin = serializers.CharField(max_length=255, required=False, allow_blank=True)
    #: A PinVisit id; must be on one of the caller's own pins.
    visit = serializers.IntegerField(required=False)


class PhotoLabelsSerializer(serializers.Serializer):
    """Validates a full replacement of one photo's media labels."""

    labels = serializers.ListField(
        child=serializers.CharField(max_length=MAX_MEDIA_LABEL_NAME_LENGTH),
        max_length=MAX_MEDIA_LABELS,
        allow_empty=True,
    )


class PhotoVoteSerializer(serializers.Serializer):
    """Validates a community relevance vote on a materialized media row."""

    #: 1 relevant, -1 not relevant, 0 withdraws an existing vote.
    value = serializers.ChoiceField(choices=[-1, 0, 1])


class PhotoVoteResponseSerializer(serializers.Serializer):
    """The item's new community score after a vote (schema-only)."""

    score = serializers.IntegerField(read_only=True)
    your_vote = serializers.IntegerField(read_only=True)


class PhotoFileSerializer(serializers.Serializer):
    """Validates filing an unfiled photo onto a pin, or onto a new one."""

    #: An existing pin (slug or uuid) to file onto. When omitted, coordinates
    #: are used to create a pin instead.
    pin = serializers.CharField(max_length=255, required=False, allow_blank=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, min_value=-90, max_value=90)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, min_value=-180, max_value=180)
    #: Name for a newly created pin; ignored when filing onto an existing one.
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)


class VisitSuggestionSerializer(serializers.Serializer):
    """One pending photo-derived visit suggestion (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    photo = PhotoSerializer(read_only=True)
    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    pin_name = serializers.CharField(read_only=True, allow_null=True)
    #: When the suggestion was raised (the row's creation time).
    suggested_at = serializers.DateTimeField(read_only=True)
    #: When the visit is claimed to have happened.
    visit_date = serializers.DateTimeField(read_only=True, allow_null=True)


class VisitSuggestionListResponseSerializer(serializers.Serializer):
    """The pending-suggestions list envelope (schema-only)."""

    suggestions = VisitSuggestionSerializer(many=True, read_only=True)


class JournalEntrySerializer(serializers.Serializer):
    """One Memories journal entry (schema-only).

    A field-for-field mirror of ``services.memories.journal.JournalEntry``.
    ``test_external_api_photos`` asserts the two stay identical, so a new
    dataclass field fails the suite instead of silently never reaching clients.
    """

    kind = serializers.CharField(read_only=True)
    occurred_at = serializers.DateTimeField(read_only=True)
    icon = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    subtitle = serializers.CharField(read_only=True, allow_blank=True)
    body = serializers.CharField(read_only=True, allow_blank=True)
    url = serializers.CharField(read_only=True)
    rating = serializers.IntegerField(read_only=True, allow_null=True)


class JournalQuerySerializer(serializers.Serializer):
    """Validates the window requested from the Memories journal."""

    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)


class JournalResponseSerializer(serializers.Serializer):
    """One window of the Memories journal (schema-only)."""

    entries = JournalEntrySerializer(many=True, read_only=True)
    #: Total entries available, for paging - the journal is materialized whole.
    total = serializers.IntegerField(read_only=True)
