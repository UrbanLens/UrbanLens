"""Deliberately minimal, hand-rolled serializers for the external API.

These never subclass or reuse the internal ``PinSerializer``/``ProfileSerializer``
(``dashboard/models/pin/serializer.py``, ``dashboard/models/profile/serializer.py``) -
the internal API is free to grow fields for the site's own frontend without
silently expanding what a third-party application is permitted to submit or
read. Field-level bounds here are the first line of defense against
untrusted input; ``services.pins.pin_creation.create_pin_for_profile`` is the
second, since it's shared with the (trusted) map UI form and sanitizes
regardless of caller.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils import timezone
from rest_framework import serializers

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.aliases.model import AliasType
from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType

# Imported at runtime, not under TYPE_CHECKING, despite being used only in
# annotations: drf-spectacular introspects every SerializerMethodField with
# typing.get_type_hints() to derive the OpenAPI type, and that resolves the
# *whole* signature - including the `obj` parameter. A name only visible to a
# type checker raises NameError there and breaks schema generation outright.
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES, KIND_CHOICES
from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status as NotificationStatus
from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.pin_list.model import PinListItem
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
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
)
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.core.text_limits import (
    MAX_COMMENT_TEXT_LENGTH,
    MAX_FRIEND_REQUEST_MESSAGE_LENGTH,
    MAX_PIN_DESCRIPTION_LENGTH,
    MAX_PIN_LIST_DESCRIPTION_LENGTH,
    MAX_PIN_NOTE_LENGTH,
    MAX_PROFILE_BIO_LENGTH,
    MAX_TRIP_ACTIVITY_NOTES_LENGTH,
    MAX_TRIP_DESCRIPTION_LENGTH,
    MAX_VISIT_NOTES_LENGTH,
)
from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison
from urbanlens.dashboard.services.map_pins.payload import MapPinPayloadService
from urbanlens.dashboard.services.media.media_labels import MAX_MEDIA_LABEL_NAME_LENGTH, MAX_MEDIA_LABELS
from urbanlens.dashboard.services.notifications.notification_center import preference_field_names
from urbanlens.dashboard.services.pins.pin_edit import EDITABLE_PIN_FIELDS
from urbanlens.dashboard.services.trips.trip_comments import ALLOWED_COMMENT_EMOJIS

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.profile.model import Profile

#: Same scheme restriction as controllers.links._clean_link_input - external
#: submissions are untrusted input, so this validates before anything else does.
_validate_link_url = URLValidator(schemes=["http", "https"])


class WhoAmISerializer(serializers.Serializer):
    """The calling key owner's own identity: their profile uuid and slug.

    Nothing else - no settings, no friends, no contact details. This is still
    the narrowest thing the API serves, but "uuid only" turned out to be one
    field short of usable.

    The slug is here because it is the *identifier every other endpoint speaks*.
    A profile is addressed by slug throughout this API
    (``/profiles/{profile_slug}/``, ``/messages/{profile_slug}/``), and payloads
    that name a person name them by slug: every direct message carries a
    ``sender_slug``, so a client holding only its own uuid literally cannot tell
    which messages in a conversation are its own. That is not a quirk of one
    endpoint - it is what happens whenever a client has to recognize itself in a
    payload it did not send. Handing over the slug at authentication time is the
    single place that answer belongs.
    """

    uuid = serializers.UUIDField(read_only=True)
    #: The caller's own profile slug - what every other endpoint's paths and
    #: ``*_slug`` payload fields use to name a person. Always present:
    #: ``WhoAmIView`` backfills it via ``Profile.ensure_slug()`` for accounts
    #: created before slugs existed.
    slug = serializers.CharField(read_only=True)


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
    #: uuid of one of the caller's own pins to create this one as a child
    #: (detail pin) of - e.g. a building entrance a few meters from its main
    #: pin. See ``services.pins.pin_creation.create_pin_for_profile``'s parent_id
    #: docstring for why this matters: without it, coordinates this close
    #: would be swallowed by the default fuzzy-location dedup instead of
    #: creating the distinct child.
    parent_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    #: Whether ``name`` is a name a human deliberately typed, rather than one
    #: a parser produced. Only the client knows which: an interactive app
    #: sends True for a name entered in its pin form, while an importer or
    #: offline outbox replaying captured data leaves it False so a coordinate
    #: string or "Dropped Pin" fallback doesn't permanently outrank the real
    #: name discovered later (see ``tasks.upgrade_placeholder_pin_names``).
    #: Defaults to False, so a client that says nothing keeps the safe
    #: importer behavior. ``PATCH`` needs no equivalent - an edit naming a pin
    #: is by definition its owner doing so deliberately.
    name_is_user_provided = serializers.BooleanField(required=False, default=False)

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
    ``services.pins.pin_suggestions.ingest_location_hits``). This is why an
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


class SyncPinTagSerializer(serializers.Serializer):
    """One label chip on a synced pin (schema-only).

    Spelled out rather than left as an untyped dict because ``kind`` is what
    lets an offline client tell a status from a category from a tag without
    re-deriving it, and a ``DictField`` would leave that invisible in the
    generated client.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    color = serializers.CharField(read_only=True, allow_null=True)
    icon = serializers.CharField(read_only=True, allow_null=True)
    #: One of ``tag``, ``category``, or ``status`` - the only kinds shown as chips.
    kind = serializers.CharField(read_only=True)


class SyncPinSerializer(serializers.Serializer):
    """Documents the pin payload shape served by the delta-sync endpoint.

    Schema-only: the actual payload is built by
    ``services.pins.pin_sync.serialize_sync_pin`` (the map payload plus sync-only
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
    tags = SyncPinTagSerializer(many=True, read_only=True)
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


class PinNoteSerializer(serializers.Serializer):
    """One personal note on a pin - both the read shape and the create payload.

    Notes are append-only by design (see ``models.pin.note.PinNote``), so
    there is no update counterpart: a client edits a note by deleting it and
    adding another. A note is also single-author and private, so it has no
    threading and no reactions - a client wanting either should point at
    ``/pins/{slug}/comments/`` instead, which already supports both.
    """

    id = serializers.IntegerField(read_only=True)
    text = serializers.CharField(max_length=MAX_PIN_NOTE_LENGTH, trim_whitespace=True, allow_blank=False)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class PinAliasSerializer(serializers.Serializer):
    """One alternate name for a pin - both the read shape and the create payload."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(max_length=255)
    kind = serializers.ChoiceField(choices=AliasType.choices, default=AliasType.ALTERNATE)
    #: Who contributed this name (the user, or an external source that
    #: discovered it). Read-only: a client cannot claim an alias came from
    #: somewhere it didn't.
    source = serializers.CharField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    is_current = serializers.SerializerMethodField()

    def get_is_current(self, alias) -> bool:
        """Whether this alias is the pin's current name.

        The pin comes from the serializer context rather than ``alias.pin`` so
        that serializing a whole list costs no per-row query.

        Args:
            alias: The alias being serialized.

        Returns:
            True when this alias matches the pin's current effective name,
            comparing loosely enough to ignore case, spacing, and punctuation.
        """
        pin = self.context.get("pin")
        if pin is None:
            return False
        current = normalize_name_for_comparison(pin.effective_name)
        return bool(current) and normalize_name_for_comparison(alias.name) == current


class PinLinkSerializer(serializers.Serializer):
    """One external link on a pin (output only).

    Separate from :class:`PinLinkCreateSerializer` because ``name`` means
    different things in each direction: on the way out it is the resolved
    ``display_name`` (which falls back to the url's host when the link was
    saved without a label), and a read-only field cannot also accept input.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(source="display_name", read_only=True)
    url = serializers.CharField(read_only=True)
    wayback_url = serializers.SerializerMethodField()
    order = serializers.IntegerField(read_only=True)
    created = serializers.DateTimeField(read_only=True)

    def get_wayback_url(self, link) -> str | None:
        """The Wayback snapshot url, or null when none has been archived yet.

        The model stores "not archived" as ``""``; this reports it as null to
        match the shape ``services.pins.pin_detail.build_pin_detail`` already ships.

        Args:
            link: The link being serialized.

        Returns:
            The snapshot url, or None.
        """
        return link.wayback_url or None


class PinLinkCreateSerializer(serializers.Serializer):
    """An external link submitted for a pin (input only)."""

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    url = serializers.URLField(max_length=MAX_LINK_URL_LENGTH)


class CustomFieldValueSerializer(serializers.Serializer):
    """One custom field's value on a target object (schema-only).

    Shared by the pin-detail payload's nested ``custom_fields`` and the
    standalone ``custom-fields/`` domain - both describe the same row shape.
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
    ``services.pins.pin_detail.build_pin_detail``, the function that actually
    builds this payload. ``test_external_api_schema.PinDetailContractTests``
    asserts these fields exactly match what that function really emits.
    """

    official_name = serializers.CharField(read_only=True, allow_null=True)
    #: Address components, split out from the combined `address` string
    #: (geocoded from the pin's Location - never user-writable).
    city = serializers.CharField(read_only=True, allow_null=True)
    state = serializers.CharField(read_only=True, allow_null=True)
    county = serializers.CharField(read_only=True, allow_null=True)
    country = serializers.CharField(read_only=True, allow_null=True)
    zipcode = serializers.CharField(read_only=True, allow_null=True)
    date_built = serializers.DateField(read_only=True, allow_null=True)
    date_abandoned = serializers.DateField(read_only=True, allow_null=True)
    date_last_active = serializers.DateField(read_only=True, allow_null=True)
    security = PinSecurityDetailSerializer(read_only=True)
    #: The slug to pass to ``/wikis/{location_slug}/``. Wiki routes resolve a
    #: *Location*, so this - not ``wiki_slug`` - is the navigable identifier.
    location_slug = serializers.CharField(read_only=True)
    #: Informational only. ``Wiki.slug`` is independent of the Location slug and
    #: is not accepted by any wiki route; use ``location_slug`` to navigate.
    wiki_slug = serializers.CharField(read_only=True, allow_null=True)
    cover_photo_url = serializers.CharField(read_only=True, allow_null=True)
    boundary = serializers.JSONField(read_only=True, allow_null=True)
    notes = PinNoteSerializer(many=True, read_only=True)
    aliases = PinAliasSerializer(many=True, read_only=True)
    links = PinLinkSerializer(many=True, read_only=True)
    custom_fields = CustomFieldValueSerializer(many=True, read_only=True)
    note_count = serializers.IntegerField(read_only=True)
    alias_count = serializers.IntegerField(read_only=True)
    link_count = serializers.IntegerField(read_only=True)


def _security_update_fields() -> dict[str, serializers.Field]:
    """Build the optional, writable counterpart of :class:`PinSecurityDetailSerializer`.

    Generated from ``models.abstract.security.SECURITY_FIELDS`` rather than
    typed out, so a ninth indicator added to the model mixin becomes writable
    here automatically instead of being silently unwritable - the same
    silent-drop failure this whole serializer widening exists to end.

    Returns:
        Mapping of field name to an optional ``SecurityLevel`` choice field.
    """
    return {name: serializers.ChoiceField(choices=SecurityLevel.choices, required=False) for name, _label in SECURITY_FIELDS}


#: The wire key carrying the nested security object in a pin-update payload.
#: Named as a constant because it collides with a ``Pin`` *column* of the same
#: name - see :meth:`PinUpdateSerializer.pin_field_edits`, which is the one
#: place that collision is resolved.
SECURITY_WIRE_KEY = "security"


PinSecurityUpdateSerializer = type(
    "PinSecurityUpdateSerializer",
    (serializers.Serializer,),
    {
        "__doc__": (
            "The 8 security indicators, as a partial update.\n\n"
            "    Every field is optional and an omitted one is left alone, so a client\n"
            '    that only learned the gate is now locked can send ``{"locked":\n'
            '    "everywhere"}`` without restating the other seven. None of them accept\n'
            '    null: ``unknown`` is this model\'s own representation of "not known",\n'
            "    and the columns are non-nullable, so clearing an indicator means\n"
            "    setting it back to ``unknown``.\n    "
        ),
        **_security_update_fields(),
    },
)


class PinUpdateSerializer(serializers.Serializer):
    """Validates an untrusted pin-update payload.

    Covers the whole of what a pin's owner can edit about it from the website's
    own pin-detail dialog, plus one addition the mobile app needs that no
    internal endpoint exposes: ``parent_id``, to detach a pin (``null``) or
    re-parent it under another of the caller's own pins (its uuid).

    Every field is optional; **absent means untouched, and an explicit null
    clears**. That distinction is the entire point of this serializer. An
    earlier version accepted only name/icon/last_visited/coordinates/parent_id
    and *silently dropped* everything else while still answering 200, so a user
    who edited a pin's description in the app saw a success and lost the edit.

    Three things a client may expect here and will not find:

    * ``rating`` is deliberately excluded. A pin's rating is not a pin field at
      all - it is the caller's ``Review`` of it, written through
      ``PUT``/``DELETE /pins/{slug}/review/``. Accepting it here as well would
      give one value two write paths that must be kept in agreement forever,
      which is strictly worse than making clients call the endpoint that owns it.
    * ``address``, ``city``, ``state`` and ``country`` are read-only. They are
      not stored on the pin: they are derived from the shared ``Location`` the
      pin points at (see ``models/CLAUDE.md`` on the Location/Pin split), and
      several people's pins can share one Location. A pin is moved by sending
      ``latitude``/``longitude``, which repoints it at a different Location -
      never by rewriting an address.
    * ``official_name`` likewise belongs to the Location, not the pin; ``name``
      here is the caller's own private label for it.

    ``priority``, ``danger`` and ``vulnerability`` are not purely private edits:
    when the owner has the matching ``sync_*_to_wiki`` setting on and the pin is
    attached to a community wiki, writing one publishes (or withdraws) their
    ``WikiStatVote`` on that wiki - see ``models.pin.signals.sync_pin_stats_to_wiki``.
    A client should surface that, which is why it is stated in this endpoint's
    OpenAPI description too.
    """

    name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    icon = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    #: The owner's personal notes on this pin. Bounded by the same limit the
    #: website's own editor enforces (``services.core.text_limits``).
    description = serializers.CharField(max_length=MAX_PIN_DESCRIPTION_LENGTH, required=False, allow_blank=True, allow_null=True)
    #: Hex color override for this pin's marker, e.g. ``"#F44336"``. Null/blank
    #: restores the inherited color (the winning label's, or the default).
    color = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)
    #: What the marker physically represents. Setting it also marks the type
    #: user-provided, which stops automatic building/parcel classification from
    #: overruling the choice later.
    pin_type = serializers.ChoiceField(choices=PinType.choices, required=False)
    #: How urgently the owner wants to visit (0 = unset, 1-5). See the class
    #: docstring: this can publish a community wiki vote.
    priority = serializers.IntegerField(required=False, min_value=0, max_value=5)
    #: How hazardous the site is (0 = unset, 1-5). Can publish a wiki vote.
    danger = serializers.IntegerField(required=False, min_value=0, max_value=5)
    #: How at-risk/fragile the site is (0 = unset, 1-5). Can publish a wiki vote.
    vulnerability = serializers.IntegerField(required=False, min_value=0, max_value=5)
    last_visited = serializers.DateTimeField(required=False, allow_null=True)
    date_built = serializers.DateField(required=False, allow_null=True)
    date_abandoned = serializers.DateField(required=False, allow_null=True)
    date_last_active = serializers.DateField(required=False, allow_null=True)
    security = PinSecurityUpdateSerializer(required=False)
    #: **Full replacement** of the pin's tag/category/status labels, by uuid -
    #: not a delta. Send the complete set the pin should end up with; sending
    #: ``[]`` removes them all. Person and media labels are untouched (they are
    #: attached by other surfaces entirely). Every label dropped by the
    #: replacement is tombstoned, so keyword/AI auto-tagging cannot quietly put
    #: it back on the next run. An unknown uuid, or one belonging to another
    #: user's private label, is a 400 - not a silent skip.
    label_uuids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True)
    #: Convenience over the "Visited" status label: true adds it, false removes
    #: it *and* clears ``last_visited``. Mutually exclusive with an explicit
    #: ``last_visited`` in the same request - see :meth:`validate`.
    visited = serializers.BooleanField(required=False)
    latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    #: A pin uuid to become this pin's new parent, or null to detach it to a
    #: top-level pin of its own. Omit entirely to leave the parent untouched.
    parent_id = serializers.UUIDField(required=False, allow_null=True)
    #: Acknowledges that the move costs the caller access to one or more
    #: community wikis. A move that would do so is refused with 409 (listing
    #: them) until this is sent - see ``PinDetailView.patch``.
    confirm_wiki_loss = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict) -> dict:
        """Reject payloads that are internally contradictory.

        Args:
            attrs: The field-validated payload.

        Returns:
            The same payload, unchanged.

        Raises:
            rest_framework.exceptions.ValidationError: Coordinates were sent
                half-present, null, or non-finite (a pin cannot be moved to
                "half a point"), or ``visited`` was combined with an explicit
                ``last_visited``. The latter is refused rather than resolved
                because the two make opposite claims about the same fact -
                ``visited: false`` clears ``last_visited`` outright - and any
                precedence rule we picked would silently discard one of the two
                things the client actually asked for.
        """
        has_lat = "latitude" in attrs
        has_lng = "longitude" in attrs
        if has_lat != has_lng:
            raise serializers.ValidationError("Provide both latitude and longitude together.")
        if has_lat and (attrs["latitude"] is None or attrs["longitude"] is None):
            raise serializers.ValidationError("latitude and longitude cannot be null.")
        if has_lat and not (math.isfinite(attrs["latitude"]) and math.isfinite(attrs["longitude"])):
            raise serializers.ValidationError("latitude and longitude must be finite numbers.")
        if "visited" in attrs and "last_visited" in attrs:
            raise serializers.ValidationError("Send either 'visited' or 'last_visited', not both - they disagree about the same fact.")
        return attrs

    def pin_field_edits(self) -> dict[str, Any]:
        """Flatten the validated payload into ``Pin`` column name -> value to write.

        Only keys this request actually submitted appear, so the result can be
        handed straight to ``services.pins.pin_edit.apply_pin_edits`` without
        breaking its absent-means-untouched contract. Everything that is not a
        ``Pin`` column (``latitude``/``longitude``, ``parent_id``,
        ``label_uuids``, ``visited``, ``confirm_wiki_loss``) is dropped here -
        each of those has its own handling in ``PinDetailView.patch``.

        The nested ``security`` object is flattened into the same mapping
        rather than given a second write path of its own, because the eight
        indicators are plain ``Pin`` columns.

        Dropping the ``security`` *wire key* from the flat copy first is
        load-bearing, not tidiness: ``security`` is **also** the name of one of
        those eight columns (see ``models.abstract.security.SECURITY_FIELDS``),
        so it passes the ``EDITABLE_PIN_FIELDS`` membership test and a naive
        copy would carry the whole nested dict through to
        ``setattr(pin, "security", {...})``. ``Pin.security`` is a
        ``varchar(20)``, so that save died with a database ``DataError`` and
        the caller got a 500 - for the entirely ordinary payload
        ``{"security": {"locked": "everywhere"}}``.

        Returns:
            Mapping of ``Pin`` field name to the value to write. Always a
            subset of ``services.pins.pin_edit.EDITABLE_PIN_FIELDS``.
        """
        data = self.validated_data
        edits: dict[str, Any] = {field: value for field, value in data.items() if field in EDITABLE_PIN_FIELDS and field != SECURITY_WIRE_KEY}
        edits.update(data.get(SECURITY_WIRE_KEY) or {})
        return edits


class PinVisitSerializer(serializers.Serializer):
    """One logged visit to a pin (output only)."""

    id = serializers.IntegerField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    visited_at = serializers.DateTimeField(read_only=True)
    notes = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    #: How the visit was recorded (manual entry, a photo's timestamp, a trip,
    #: geolocation, ...). Read-only in v1: this endpoint only creates manual
    #: visits, and a client cannot claim one came from somewhere else.
    source = serializers.CharField(read_only=True)
    #: Set on visits inferred rather than confirmed. Read-only in v1 for the
    #: same reason as ``source``.
    tentative = serializers.BooleanField(read_only=True)
    #: Photos attached to this visit. Served from a queryset annotation, not a
    #: per-row count query.
    photo_count = serializers.IntegerField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class PinVisitCreateSerializer(serializers.Serializer):
    """A manually logged visit submitted for a pin."""

    visited_at = serializers.DateTimeField(required=True)
    notes = serializers.CharField(max_length=MAX_VISIT_NOTES_LENGTH, required=False, allow_blank=True, allow_null=True, default=None)


class LocationSearchQuerySerializer(serializers.Serializer):
    """Validates the query params of the location autocomplete endpoint."""

    q = serializers.CharField(max_length=200, required=True, trim_whitespace=True)
    #: Comma-separated subset of ``local`` (the caller's own pins) and
    #: ``places`` (the configured external places provider). Unknown entries
    #: are ignored rather than rejected, so a newer client asking for a source
    #: this server doesn't have still gets the sources it does.
    sources = serializers.CharField(required=False, default="local,places")
    limit = serializers.IntegerField(required=False, min_value=1, max_value=25, default=15)


class LocationSearchResultSerializer(serializers.Serializer):
    """One autocomplete hit (schema-only).

    Mirrors ``services.map_pins.autocomplete.AutocompleteResult.to_dict()``,
    which is what actually builds these - the same wire shape the web map's
    own autocomplete consumes.
    """

    #: What kind of hit this is, e.g. a local pin or an external place.
    type = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    subtitle = serializers.CharField(read_only=True, allow_blank=True)
    #: Null for a place that has not been resolved to coordinates yet - call
    #: ``locations/resolve/`` with its ``place_id`` to get them.
    lat = serializers.FloatField(read_only=True, allow_null=True)
    lng = serializers.FloatField(read_only=True, allow_null=True)
    zoom = serializers.IntegerField(read_only=True)
    icon = serializers.CharField(read_only=True, allow_null=True)
    #: Set on local hits: the pin this result refers to.
    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    #: Set on external hits: the provider's opaque id, for ``locations/resolve/``.
    place_id = serializers.CharField(read_only=True, allow_null=True)
    #: True when this local hit is a child pin rather than a top-level one.
    is_child = serializers.BooleanField(read_only=True)


class LocationSearchResponseSerializer(serializers.Serializer):
    """Documents the envelope of the location autocomplete endpoint (schema-only)."""

    results = LocationSearchResultSerializer(many=True, read_only=True)
    #: True when external place results were requested but not served - either
    #: the caller turned external lookups off, or no provider is configured.
    #: A client shows "searching your pins only" rather than an empty state.
    places_disabled = serializers.BooleanField(read_only=True)


class PlaceResolveResponseSerializer(serializers.Serializer):
    """Documents the place-resolution response (schema-only)."""

    lat = serializers.FloatField(read_only=True, allow_null=True)
    lng = serializers.FloatField(read_only=True, allow_null=True)
    name = serializers.CharField(read_only=True, allow_blank=True)


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
    parent_uuid = serializers.UUIDField(read_only=True, allow_null=True)


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
    added to ``services.profile.profile_settings.SETTINGS_FIELDS`` and to this class.

    Fields mirror that allowlist exactly; the trailing read-only keys are
    computed context (see ``services.profile.profile_settings.read_settings``).
    """

    # Name (User passthrough).
    first_name = serializers.CharField(read_only=True, allow_blank=True)
    last_name = serializers.CharField(read_only=True, allow_blank=True)
    # Contact methods.
    phone_number = serializers.CharField(read_only=True, allow_blank=True)
    signal_username = serializers.CharField(read_only=True, allow_blank=True)
    discord_username = serializers.CharField(read_only=True, allow_blank=True)
    whatsapp_number = serializers.CharField(read_only=True, allow_blank=True)
    telegram_username = serializers.CharField(read_only=True, allow_blank=True)
    matrix_handle = serializers.CharField(read_only=True, allow_blank=True)
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
    track_device_scans = serializers.BooleanField(read_only=True)
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

    # Name (User passthrough) - blank clears to "", matching User's own default.
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    # Contact methods. discord_username's charset is checked in
    # services.profile.profile_settings (mirrors ContactMethodsForm.clean_discord_username).
    phone_number = serializers.CharField(required=False, allow_blank=True, max_length=30)
    signal_username = serializers.CharField(required=False, allow_blank=True, max_length=100)
    discord_username = serializers.CharField(required=False, allow_blank=True, max_length=100)
    whatsapp_number = serializers.CharField(required=False, allow_blank=True, max_length=30)
    telegram_username = serializers.CharField(required=False, allow_blank=True, max_length=100)
    matrix_handle = serializers.CharField(required=False, allow_blank=True, max_length=200)
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
    track_device_scans = serializers.BooleanField(required=False)
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
    #: caller's plan entitlement in services.profile.profile_settings.
    image_downscale_max_dimension = serializers.IntegerField(required=False, allow_null=True)
    video_downscale_max_height = serializers.IntegerField(required=False, allow_null=True)


#: Upper bound on the total vertex count of a submitted smart-list boundary.
#: A MultiPolygon is stored verbatim and re-tested against every one of the
#: owner's pins on each resync, so an unbounded one is both a storage and a CPU
#: amplification vector. Generous enough for any hand-drawn or imported region;
#: tight enough that a pathological payload is refused rather than persisted.
MAX_BOUNDARY_VERTICES = 20_000

#: Human-readable description of the ``criteria``/``smart_filter`` JSON shape,
#: reused by every field carrying it. Deliberately describes the *existing*
#: format produced by ``services.search.filter_criteria.serialize_form_criteria`` -
#: this is not a new contract, and the two must not drift.
CRITERIA_HELP_TEXT = (
    "Saved main-map filter criteria, in the same JSON shape "
    "`services.search.filter_criteria.serialize_form_criteria` produces and "
    "`deserialize_criteria` replays. Every key is optional; an absent key means "
    '"no filter on that dimension". Recognized keys: `name` (substring match); '
    "the numeric bounds `min_rating`/`max_rating`, `min_priority`/`max_priority`, "
    "`min_danger`/`max_danger`, `min_vulnerability`/`max_vulnerability`, "
    "`min_detail_pins`/`max_detail_pins`; the flags `has_visits`, `has_links`, "
    "`overlapping_pins`; the eight `security_*` indicators (`security_fences`, "
    "`security_alarms`, `security_cameras`, `security_security`, `security_signs`, "
    "`security_vps`, `security_plywood`, `security_locked`); ISO-8601 date bounds "
    "`visited_after`/`visited_before`, `created_after`/`created_before`, "
    "`date_built_after`/`date_built_before`, `date_abandoned_after`/"
    "`date_abandoned_before`, `last_viewed_after`/`last_viewed_before`; "
    "`tags`/`exclude_tags` (arrays of label ids); `label_groups` (array of "
    '`{"op": "and"|"or"|"not", "ids": [label id, ...]}`, which takes precedence '
    "over `tags`/`exclude_tags` when present); `custom_fields` (array of "
    "`{field_id, ...}` bound objects); and `include_regions`/`exclude_regions` "
    "(GeoJSON geometries). Every label id must be visible to you and every "
    "custom-field id must be your own, or the write is refused."
)


class PinSummarySerializer(serializers.Serializer):
    """The minimal pin identity nested inside list-membership payloads.

    Deliberately far smaller than :class:`SyncPinSerializer`: an items page is
    about *which* pins are on a list and in what order, and a client that wants
    a pin's full detail already has ``GET pins/{slug}/`` for that. Keeping this
    small is what makes a 100-item page cheap.
    """

    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    #: The pin's display name with the owner's aliases/overrides applied.
    name = serializers.CharField(read_only=True, source="effective_name")
    latitude = serializers.FloatField(read_only=True, allow_null=True)
    longitude = serializers.FloatField(read_only=True, allow_null=True)


class PinListSerializer(serializers.Serializer):
    """One of the caller's pin lists, as served by the list endpoint.

    ``smart_boundary`` is reported only as the boolean ``has_boundary`` here.
    The polygon itself can be megabytes, and a page of 25 lists would be
    dominated by geometry the caller almost certainly does not need - fetch the
    detail endpoint for one list to get it.
    """

    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True, allow_blank=True)
    #: When true, membership is recomputed from smart_filter/smart_boundary as
    #: pins change; when false the list only ever changes by explicit edits.
    is_smart = serializers.BooleanField(read_only=True)
    pin_count = serializers.IntegerField(read_only=True)
    smart_filter = serializers.JSONField(read_only=True, allow_null=True, help_text=CRITERIA_HELP_TEXT)
    has_boundary = serializers.SerializerMethodField()
    source_saved_filter_uuid = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)

    def get_has_boundary(self, obj) -> bool:
        """Whether this list has a drawn boundary, without serializing it."""
        return obj.smart_boundary is not None

    def get_source_saved_filter_uuid(self, obj) -> str | None:
        """The uuid of the SavedFilter this list's criteria were copied from, if any."""
        return str(obj.source_saved_filter.uuid) if obj.source_saved_filter_id else None


class PinListDetailSerializer(PinListSerializer):
    """One pin list including its full boundary geometry."""

    smart_boundary = serializers.SerializerMethodField()

    def get_smart_boundary(self, obj) -> dict | None:
        """The boundary as a GeoJSON MultiPolygon, or null when unset."""
        from urbanlens.dashboard.services.geo.geo import geometry_to_geojson

        return geometry_to_geojson(obj.smart_boundary)


class PinListWriteSerializer(serializers.Serializer):
    """Validates an untrusted pin-list create/update payload.

    Used for both POST and PATCH; the view passes ``partial=True`` for the
    latter, so presence in ``validated_data`` is what distinguishes "not
    submitted" from "set to null".
    """

    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, max_length=MAX_PIN_LIST_DESCRIPTION_LENGTH)
    is_smart = serializers.BooleanField(required=False)
    smart_filter = serializers.JSONField(required=False, allow_null=True, help_text=CRITERIA_HELP_TEXT)
    #: A GeoJSON Polygon or MultiPolygon. Converted to a MultiPolygon on the
    #: way in (see services.geo.geo.parse_multipolygon_geojson), so a client may
    #: submit either.
    smart_boundary = serializers.JSONField(required=False, allow_null=True)
    #: Point this list at one of the caller's saved filters: its criteria are
    #: copied into smart_filter, and later edits to that filter resync this
    #: list. Null detaches the list from its source.
    source_saved_filter_uuid = serializers.UUIDField(required=False, allow_null=True)

    def validate_smart_boundary(self, value):
        """Parse and bound the submitted boundary geometry.

        Args:
            value: A GeoJSON Polygon/MultiPolygon dict, or None to clear it.

        Returns:
            The parsed ``MultiPolygon``, or None.

        Raises:
            serializers.ValidationError: If the payload is not polygonal
                GeoJSON, or carries more vertices than
                :data:`MAX_BOUNDARY_VERTICES`.
        """
        if value is None:
            return None
        from urbanlens.dashboard.services.geo.geo import parse_multipolygon_geojson

        try:
            geom = parse_multipolygon_geojson(value)
        except TypeError as exc:
            raise serializers.ValidationError("Boundary must be a GeoJSON Polygon or MultiPolygon.") from exc
        except ValueError as exc:
            raise serializers.ValidationError("Boundary is not valid GeoJSON geometry.") from exc

        if geom.num_points > MAX_BOUNDARY_VERTICES:
            raise serializers.ValidationError(f"Boundary is too detailed - use at most {MAX_BOUNDARY_VERTICES} points.")
        return geom


class PinListItemSerializer(serializers.Serializer):
    """One pin's membership in a list, with its position and provenance."""

    id = serializers.IntegerField(read_only=True)
    order = serializers.IntegerField(read_only=True)
    #: How the pin got here. "manual" memberships are never removed by an
    #: automatic resync, unlike "smart_filter"/"boundary" ones.
    added_via = serializers.ChoiceField(choices=PinListItem.ADDED_VIA_CHOICES, read_only=True)
    pin = PinSummarySerializer(read_only=True)


class PinListItemsWriteSerializer(serializers.Serializer):
    """Validates a request to add pins to a list.

    Unknown or foreign uuids are dropped by the view rather than rejected here
    - an offline client replaying a queued batch should not have the whole
    batch fail because one pin was deleted on another device meanwhile.
    """

    pin_uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)


class PinListItemsDeleteSerializer(serializers.Serializer):
    """Validates a request to remove pins from a list."""

    pin_uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1)


class PinListItemsReorderSerializer(serializers.Serializer):
    """Validates a request to renumber a list's items.

    Takes ``PinListItem`` ids (from ``PinListItemSerializer.id``), not pin
    uuids - the ordering belongs to the membership row, and one pin can sit on
    many lists.
    """

    item_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=1000)


class PinListQuerySerializer(serializers.Serializer):
    """Validates the query params of the pin-lists browse endpoint."""

    #: Restrict to smart lists (true) or plain ones (false). Omit for both.
    is_smart = serializers.BooleanField(required=False, allow_null=True, default=None)


class LabelQuerySerializer(serializers.Serializer):
    """Validates the query params of the labels browse endpoint."""

    kind = serializers.ChoiceField(choices=KIND_CHOICES, required=False)
    #: Restrict to site-wide labels (true) or the caller's own (false).
    is_global = serializers.BooleanField(required=False, allow_null=True, default=None)
    #: Case-insensitive substring match against the label's own name.
    q = serializers.CharField(required=False, allow_blank=True, max_length=255)
    #: Restrict to the immediate children of this label.
    parent_uuid = serializers.UUIDField(required=False, allow_null=True)
    #: Opt in to pin_count/location_count. Off by default because each is a
    #: correlated subquery evaluated per row.
    with_counts = serializers.BooleanField(required=False, default=False)


class PinListItemsRemoveResponseSerializer(serializers.Serializer):
    """Documents the remove-pins response (schema-only)."""

    removed = serializers.IntegerField(read_only=True)


class PinListItemsReorderResponseSerializer(serializers.Serializer):
    """Documents the reorder response (schema-only)."""

    reordered = serializers.IntegerField(read_only=True)


class PinListItemsAddResponseSerializer(serializers.Serializer):
    """Documents the add-pins response (schema-only)."""

    added = serializers.IntegerField(read_only=True)
    #: Pins that would have been added but were dropped at the per-list cap.
    skipped_over_cap = serializers.IntegerField(read_only=True)
    #: The cap in force (0 = unlimited).
    max_pins = serializers.IntegerField(read_only=True)


class PinListResyncResponseSerializer(serializers.Serializer):
    """Documents the smart-list resync response (schema-only)."""

    pin_count = serializers.IntegerField(read_only=True)


class PinListMarkupMapResponseSerializer(serializers.Serializer):
    """Documents the list markup-map create/refresh response (schema-only)."""

    markup_map_uuid = serializers.UUIDField(read_only=True)


class SavedFilterSerializer(serializers.Serializer):
    """One of the caller's saved main-map filters."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    icon = serializers.CharField(read_only=True, allow_blank=True)
    criteria = serializers.JSONField(read_only=True, help_text=CRITERIA_HELP_TEXT)
    order = serializers.IntegerField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class SavedFilterWriteSerializer(serializers.Serializer):
    """Validates an untrusted saved-filter create/update payload."""

    name = serializers.CharField(max_length=100)
    icon = serializers.CharField(required=False, allow_blank=True, max_length=64)
    criteria = serializers.JSONField(required=False, help_text=CRITERIA_HELP_TEXT)
    order = serializers.IntegerField(required=False)

    def validate_criteria(self, value):
        """Require a JSON object, since every consumer indexes it by key.

        Args:
            value: The submitted criteria.

        Returns:
            The validated criteria dict.

        Raises:
            serializers.ValidationError: If it isn't a JSON object.
        """
        if not isinstance(value, dict):
            raise serializers.ValidationError("criteria must be a JSON object.")
        return value


class SavedFilterUpdateResponseSerializer(SavedFilterSerializer):
    """A saved filter plus how many derived lists its edit resynced (schema-only)."""

    #: Smart lists whose membership was recomputed because they were derived
    #: from this filter and its criteria changed. See
    #: ``services.pins.pin_list_membership.resync_lists_for_saved_filter``.
    lists_resynced = serializers.IntegerField(read_only=True)


class LabelSerializer(serializers.Serializer):
    """One label visible to the caller, with their own customizations applied.

    The ``effective_*`` fields are the values that should actually be
    displayed: a per-profile ``LabelCustomization`` override where one exists,
    otherwise the label's own value. They are only correct when the queryset
    was built with ``.with_customizations_for(profile)`` - see
    ``external_api.views.LabelsView``.
    """

    uuid = serializers.UUIDField(read_only=True)
    #: The label's own stored name, ignoring any customization.
    name = serializers.CharField(read_only=True)
    #: The name to display - the caller's override if they have one.
    effective_name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    kind = serializers.ChoiceField(choices=KIND_CHOICES, read_only=True)
    color = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    effective_color = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    icon = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    effective_icon = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    custom_icon_url = serializers.SerializerMethodField()
    order = serializers.IntegerField(read_only=True)
    #: Protected labels (e.g. the built-in "Visited" status) cannot be edited,
    #: deleted, or merged away.
    is_protected = serializers.BooleanField(read_only=True)
    allow_auto_tag = serializers.BooleanField(read_only=True)
    keywords = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    is_global = serializers.SerializerMethodField()
    is_customized = serializers.BooleanField(read_only=True)
    is_editable = serializers.SerializerMethodField()
    parent_uuids = serializers.SerializerMethodField()
    #: Present only when the caller asked for counts (``?with_counts=true``) -
    #: they cost a correlated subquery per label.
    pin_count = serializers.IntegerField(read_only=True, required=False)
    location_count = serializers.IntegerField(read_only=True, required=False)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)

    def get_custom_icon_url(self, obj) -> str | None:
        """The uploaded icon image's url, or null when the label uses none."""
        return obj.custom_icon.url if obj.custom_icon else None

    def get_is_global(self, obj) -> bool:
        """Whether this label is shared by every user rather than owned by one."""
        return obj.profile_id is None

    def get_is_editable(self, obj) -> bool:
        """Whether the caller may modify the label itself (as opposed to customizing it)."""
        return obj.profile_id is not None and not obj.is_protected

    def get_parent_uuids(self, obj) -> list[str]:
        """The uuids of this label's immediate parents in the hierarchy."""
        return [str(parent.uuid) for parent in obj.parents.all()]


class LabelWriteSerializer(serializers.Serializer):
    """Validates an untrusted label create/update payload.

    ``kind`` is required on create and ignored on update: converting a label
    between kinds moves it between entirely different attachment surfaces
    (pins, images, profiles) and is deliberately out of scope for this API.
    """

    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    kind = serializers.ChoiceField(choices=KIND_CHOICES, required=False)
    color = serializers.ChoiceField(choices=COLOR_CHOICES, required=False, allow_null=True, allow_blank=True)
    icon = serializers.CharField(max_length=50, required=False, allow_blank=True, allow_null=True)
    order = serializers.IntegerField(required=False)
    allow_auto_tag = serializers.BooleanField(required=False)
    keywords = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    #: Uuids of labels to become this label's parents. Replaces the existing
    #: set. Any that would close a hierarchy cycle are refused.
    parent_uuids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=50)


class LabelCustomizationSerializer(serializers.Serializer):
    """Validates a per-profile display override for a label.

    All three fields are optional and nullable. An empty string is normalized
    to null by the service, so "" and null both mean "no override"; when all
    three end up empty the customization row is deleted outright.
    """

    name = serializers.CharField(max_length=255, required=False, allow_null=True, allow_blank=True)
    icon = serializers.CharField(max_length=50, required=False, allow_null=True, allow_blank=True)
    color = serializers.CharField(max_length=50, required=False, allow_null=True, allow_blank=True)


class LabelMergeSerializer(serializers.Serializer):
    """Validates a label-merge request.

    The target is the label named in the URL; these are the labels that will be
    consumed and deleted.
    """

    source_uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=100)


class LabelMergeResponseSerializer(serializers.Serializer):
    """Documents the label-merge response (schema-only)."""

    target = LabelSerializer(read_only=True)
    merged_uuids = serializers.ListField(child=serializers.UUIDField(), read_only=True)
    pins_moved = serializers.IntegerField(read_only=True)


class SafetyCheckinContactSerializer(serializers.Serializer):
    """One emergency contact on a check-in (read-only).

    **The ``token`` field is deliberately absent and must stay that way.** That
    uuid is the sole credential for the tokenized contact portal
    (``safety.contact.portal``), which is intentionally session-free so a
    contact with no account can open it straight from an email. Anyone holding
    the token can read the check-in, post to its chat, and mark the owner safe.
    Emitting it here would let any ``safety:read`` key mint portal access for
    every contact - and to an outside observer that access is indistinguishable
    from the real contact acting. ``SafetyContactTokenExposureTests`` asserts it
    never appears in any payload.
    """

    id = serializers.IntegerField(read_only=True)
    display_name = serializers.SerializerMethodField()
    email = serializers.EmailField(read_only=True, allow_null=True)
    username = serializers.SerializerMethodField()
    notified_at = serializers.DateTimeField(read_only=True, allow_null=True)
    found_safe_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def get_display_name(self, obj: SafetyCheckinContact) -> str:
        """Return the contact's best display label: linked username, else saved name, else email."""
        if obj.contact_profile is not None:
            return obj.contact_profile.username
        return obj.name or obj.email or ""

    def get_username(self, obj: SafetyCheckinContact) -> str | None:
        """Return the linked account's username, or null for an email-only contact."""
        return obj.contact_profile.username if obj.contact_profile is not None else None


class SafetyCheckinPartnerSerializer(serializers.Serializer):
    """One partner on a check-in (read-only).

    ``id`` is the ``{partnerId}`` path segment of
    ``DELETE safety/checkins/{slug}/partners/{partnerId}/``.
    """

    id = serializers.IntegerField(read_only=True)
    username = serializers.SerializerMethodField()
    profile_uuid = serializers.SerializerMethodField()
    status = serializers.ChoiceField(choices=SafetyCheckinPartnerStatus.choices, read_only=True)
    accepted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    invited_by_username = serializers.SerializerMethodField()

    def get_username(self, obj: SafetyCheckinPartner) -> str | None:
        """Return the partner's username."""
        return obj.profile.username if obj.profile is not None else None

    def get_profile_uuid(self, obj: SafetyCheckinPartner) -> str | None:
        """Return the partner's profile uuid."""
        return str(obj.profile.uuid) if obj.profile is not None else None

    def get_invited_by_username(self, obj: SafetyCheckinPartner) -> str | None:
        """Return the username of whoever sent the invite."""
        return obj.invited_by.username if obj.invited_by is not None else None


class SafetyCheckinSummarySerializer(serializers.Serializer):
    """A check-in as it appears in the list endpoint.

    Carries the lifecycle state a client needs to render a row and decide which
    actions are still available, without the plan text, contact list, or any of
    the other detail-only PII.
    """

    uuid = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True, allow_null=True)
    title = serializers.CharField(read_only=True, allow_blank=True)
    status = serializers.ChoiceField(choices=SafetyCheckinStatus.choices, read_only=True)
    checkin_by = serializers.DateTimeField(read_only=True)
    grace_period_seconds = serializers.SerializerMethodField()
    escalated_at = serializers.DateTimeField(read_only=True, allow_null=True)
    resolved_at = serializers.DateTimeField(read_only=True, allow_null=True)
    is_resolved = serializers.BooleanField(read_only=True)
    contacts_locked = serializers.BooleanField(read_only=True)
    notifications_locked = serializers.BooleanField(read_only=True)
    is_archived = serializers.SerializerMethodField()
    destination_latitude = serializers.SerializerMethodField()
    destination_longitude = serializers.SerializerMethodField()
    trip_slug = serializers.SerializerMethodField()
    contact_count = serializers.IntegerField(read_only=True)
    partner_count = serializers.IntegerField(read_only=True)

    def get_grace_period_seconds(self, obj: SafetyCheckin) -> int | None:
        """Return the grace period as whole seconds.

        Never emit the raw ``DurationField``: DRF renders it as Django's
        ``[DD] [HH:[MM:]]ss[.uuuuuu]`` string, which no mobile client parses
        without bespoke code. An integer second count is unambiguous.
        """
        return int(obj.grace_period.total_seconds()) if obj.grace_period is not None else None

    def get_is_archived(self, obj: SafetyCheckin) -> bool:
        """Whether the check-in's PII has been sealed into its encrypted archive."""
        return hasattr(obj, "archive")

    def get_destination_latitude(self, obj: SafetyCheckin) -> float | None:
        """Return the destination latitude as a float (the model stores a Decimal)."""
        return float(obj.destination_latitude) if obj.destination_latitude is not None else None

    def get_destination_longitude(self, obj: SafetyCheckin) -> float | None:
        """Return the destination longitude as a float (the model stores a Decimal)."""
        return float(obj.destination_longitude) if obj.destination_longitude is not None else None

    def get_trip_slug(self, obj: SafetyCheckin) -> str | None:
        """Return the slug of the trip this check-in is scoped to, if any."""
        return obj.trip.slug if obj.trip is not None else None


class SafetyCheckinDetailSerializer(SafetyCheckinSummarySerializer):
    """The full check-in document, including the plan and contact list.

    Every ``live_location_*`` field is deliberately omitted this pass. Live
    location is a continuously-updating precise position stream; exposing it
    read-only through a long-lived bearer credential is a materially different
    privacy proposition from the rest of this surface and wants its own scope
    and design, not a field quietly appended here.
    """

    plan_details = serializers.CharField(read_only=True, allow_blank=True)
    contact_message = serializers.CharField(read_only=True, allow_blank=True)
    notify_community_wiki = serializers.BooleanField(read_only=True)
    wiki_notified_at = serializers.DateTimeField(read_only=True, allow_null=True)
    resolved_by_label = serializers.CharField(read_only=True, allow_blank=True)
    contacts = SafetyCheckinContactSerializer(many=True, read_only=True)
    partners = SafetyCheckinPartnerSerializer(many=True, read_only=True)
    markup_map_uuid = serializers.SerializerMethodField()
    attached_map_uuids = serializers.SerializerMethodField()
    photo_count = serializers.SerializerMethodField()

    def get_markup_map_uuid(self, obj: SafetyCheckin) -> str | None:
        """Return the uuid of the check-in's primary (drawn) route map, if it has one."""
        return str(obj.markup_map.uuid) if obj.markup_map is not None else None

    def get_attached_map_uuids(self, obj: SafetyCheckin) -> list[str]:
        """Return the uuids of every additional reference map attached to the check-in."""
        return [str(uuid) for uuid in obj.markup_maps.values_list("uuid", flat=True)]

    def get_photo_count(self, obj: SafetyCheckin) -> int:
        """Return how many photos are attached to this check-in."""
        return Image.objects.filter(safety_checkin=obj).count()


class SafetyContactInputSerializer(serializers.Serializer):
    """One submitted emergency contact: either an existing connection or a raw email.

    Mirrors ``SafetyCheckinContact``'s own exactly-one-of ``CheckConstraint`` -
    a contact is either a linked account or an email address, never both and
    never neither.
    """

    username = serializers.CharField(max_length=150, required=False, allow_blank=False)
    email = serializers.EmailField(required=False)
    name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")

    def validate(self, attrs: dict) -> dict:
        """Require exactly one of username/email."""
        has_username = bool((attrs.get("username") or "").strip())
        has_email = bool((attrs.get("email") or "").strip())
        if has_username == has_email:
            raise serializers.ValidationError("Provide exactly one of username or email.")
        return attrs


class SafetyCheckinCreateSerializer(serializers.Serializer):
    """Validates an untrusted safety check-in creation payload."""

    title = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    checkin_by = serializers.DateTimeField()
    #: Bounded to the same 15-minute floor the web form enforces
    #: (``controllers.safety._parse_grace_period`` clamps to 0.25h), and capped
    #: at a week so a typo can't schedule an escalation years out.
    grace_period_seconds = serializers.IntegerField(required=False, min_value=900, max_value=604800)
    plan_details = serializers.CharField(max_length=20000, required=False, allow_blank=True, default="")
    contact_message = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")
    destination_latitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-90, max_value=90)
    destination_longitude = serializers.FloatField(required=False, allow_null=True, default=None, min_value=-180, max_value=180)
    #: Slug of a trip the caller has joined; scopes the active-check-in exclusivity check.
    trip = serializers.CharField(max_length=255, required=False, allow_null=True, default=None)
    notify_community_wiki = serializers.BooleanField(required=False, default=False)
    #: Omitted/null means "use my saved default contacts", exactly as the
    #: creation page prefills them. An explicit empty list means "no contacts" -
    #: a real choice (a check-in that only nags the owner), and one that must not
    #: silently resurrect the defaults.
    contacts = SafetyContactInputSerializer(many=True, required=False, allow_null=True, default=None)
    markup_map = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate_checkin_by(self, value: datetime.datetime) -> datetime.datetime:
        """Require a timezone-aware deadline strictly in the future."""
        if timezone.is_naive(value):
            raise serializers.ValidationError("checkin_by must include a timezone offset.")
        if value <= timezone.now():
            raise serializers.ValidationError("checkin_by must be in the future.")
        return value

    def validate(self, attrs: dict) -> dict:
        """Coordinates are set together or not at all."""
        has_lat = attrs.get("destination_latitude") is not None
        has_lng = attrs.get("destination_longitude") is not None
        if has_lat != has_lng:
            raise serializers.ValidationError("Provide both destination_latitude and destination_longitude together.")
        return attrs


class SafetyCheckinUpdateSerializer(serializers.Serializer):
    """Validates a partial safety check-in update - the six autosave fields.

    **No field carries a ``default``, and that is load-bearing.** The view builds
    its service kwargs purely from ``key in validated_data``, because
    ``apply_checkin_edit`` distinguishes "not submitted, leave untouched" from
    "explicitly set to this value". A default would make an absent field look
    submitted, which for a *locked* field fabricates a warning the client never
    earned, and for an unlocked one silently overwrites a value the caller never
    mentioned - the same partial-update trap :class:`SettingsPatchSerializer`
    documents.

    Contacts are not editable here: they are frozen the moment notifications lock,
    and replacing them wholesale is not an autosave-shaped operation.
    """

    title = serializers.CharField(max_length=200, required=False)
    plan_details = serializers.CharField(max_length=20000, required=False, allow_blank=True)
    contact_message = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    destination_latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    destination_longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    notify_community_wiki = serializers.BooleanField(required=False)

    def validate(self, attrs: dict) -> dict:
        """Coordinates move together or not at all."""
        if ("destination_latitude" in attrs) != ("destination_longitude" in attrs):
            raise serializers.ValidationError("Provide both destination_latitude and destination_longitude together.")
        return attrs


class SafetyPartnerInviteSerializer(serializers.Serializer):
    """Validates a partner invitation by username."""

    username = serializers.CharField(max_length=150)


class SafetyContactDefaultsSerializer(serializers.Serializer):
    """Validates a whole-list replacement of the caller's default emergency contacts."""

    contacts = SafetyContactInputSerializer(many=True)


class SafetyDefaultContactSerializer(serializers.Serializer):
    """One saved default emergency contact (schema-only).

    Thinner than :class:`SafetyCheckinContactSerializer`: a *default* is not
    attached to any check-in, so it has no notification state - and, like every
    contact payload here, no portal token.
    """

    display_name = serializers.CharField(read_only=True, allow_blank=True)
    email = serializers.EmailField(read_only=True, allow_null=True)
    username = serializers.CharField(read_only=True, allow_null=True)


class SafetyContactDefaultsResponseSerializer(serializers.Serializer):
    """The saved default contacts, plus anything that was refused (schema-only)."""

    contacts = SafetyDefaultContactSerializer(many=True, read_only=True)
    rejected = serializers.ListField(child=serializers.CharField(), read_only=True)


class SafetyPreferenceSerializer(serializers.Serializer):
    """The caller's safety defaults: message, grace period, and auto-delete window."""

    default_message = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    default_grace_period_seconds = serializers.IntegerField(required=False, min_value=900, max_value=604800)
    #: Null means "never auto-delete".
    auto_delete_after_days = serializers.IntegerField(required=False, allow_null=True, min_value=1)


class SafetyCheckinListResponseSerializer(serializers.Serializer):
    """Documents the paginated check-in list envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = SafetyCheckinSummarySerializer(many=True, read_only=True)


class SafetyPhotoSerializer(serializers.Serializer):
    """One photo attached to a safety check-in (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    caption = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)
    url = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)

    def get_url(self, obj: Image) -> str | None:
        """Return the stored file's url, or null if the file is missing."""
        return obj.image.url if obj.image else None


class SafetyPhotoListResponseSerializer(serializers.Serializer):
    """Documents the paginated check-in photo list envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = SafetyPhotoSerializer(many=True, read_only=True)


class SafetyPhotoAttachSerializer(serializers.Serializer):
    """Attaches an already-uploaded image to a check-in by uuid.

    Interim shape: the multipart upload pipeline (quota accounting, checksum
    dedup, downscaling, EXIF handling) lives in
    ``controllers.safety.SafetyGalleryView.post`` and has not yet been extracted
    into the shared ``services.photos.photo_upload`` the Photos domain is landing.
    Rebuilding it here would fork that logic and give the external surface its
    own subtly different quota and dedup behavior, so this endpoint deliberately
    only *references* an image the caller already uploaded.
    """

    image_uuid = serializers.UUIDField()


class SafetyMapSerializer(serializers.Serializer):
    """One map linked to a check-in (schema-only)."""

    uuid = serializers.UUIDField(read_only=True)
    title = serializers.CharField(read_only=True, allow_blank=True)
    #: True for the check-in's own drawn route map, false for attached reference maps.
    is_primary = serializers.BooleanField(read_only=True)


class SafetyMapListResponseSerializer(serializers.Serializer):
    """Documents the paginated check-in maps list envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = SafetyMapSerializer(many=True, read_only=True)


class SafetyMapAttachSerializer(serializers.Serializer):
    """Attaches one of the caller's existing maps to a check-in as a reference map."""

    map_uuid = serializers.UUIDField()


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
    through ``services.profile.identity_visibility.resolve_visible_identity`` so a
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
    #: Whether this relationship is marked muted. Read the flag, never
    #: ``status``: mute used to be written *over* ``status``, which un-friended
    #: the pair for every gate reading ``Profile.are_friends``. It is now a
    #: separate boolean and ``status`` is left alone.
    #:
    #: **Two caveats a client must not paper over.** First, the flag lives on
    #: the single shared row joining the pair, so it is not per-viewer - label
    #: it "muted", never "muted by you". Second, and more important:
    #: *friendship-level mute does not currently suppress anything*. No
    #: notification delivery path consults it yet (see ``docs/PROBLEMS.md``,
    #: 2026-07-28), so the muter still receives friend-request, pin-share,
    #: trip-invite and safety notifications from that profile. The preference is
    #: recorded faithfully and will start being honored when delivery is wired
    #: up; until then a UI that promises silence would be lying. The two mute
    #: mechanisms that *do* work are unrelated: ``DirectMessageMute``
    #: (per-sender DM mute) and per-group chat mute.
    is_muted = serializers.BooleanField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class FriendMuteSerializer(serializers.Serializer):
    """The desired mute state for a relationship.

    An explicit target rather than a toggle. A toggle is unsafe over a mobile
    link: a request that succeeds server-side but whose response is lost gets
    retried by the client and silently *inverts* the state it was trying to
    set, so the user ends up unmuted by the very retry meant to mute them. With
    an explicit target the retry is idempotent, which is also why the service
    functions underneath are no-ops when already in the requested state.
    """

    is_muted = serializers.BooleanField(required=True)


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
    ``services.social.friendship.invite_by_email``. Anything that made this field (or
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


class ProfileUpdateSerializer(serializers.Serializer):
    """Validates a partial update to the caller's own profile.

    Deliberately limited to the three fields that are *public presentation* -
    what other people see on your profile page. Everything else a profile row
    happens to carry is a setting, and settings are written through
    ``PATCH /settings/`` behind the ``settings:write`` scope.

    That split is a privilege boundary, not tidiness. ``PATCH /profiles/{slug}/``
    is gated on ``social:write`` - the scope an app asks for to send friend
    requests and keep a note on someone. This serializer previously also
    accepted ``theme_mode``, ``distance_units``, ``community_enabled`` and all
    twelve ``*_visibility`` fields, every one of which is already writable via
    ``PATCH /settings/``. A credential holding only ``social:write`` could
    therefore rewrite every privacy-visibility field on the account - turning
    ``profile_visibility`` to ``everyone``, say - which is exactly the surface
    ``settings:write`` exists to protect. ``ProfileSettingsOverlapTests`` asserts
    the two field sets stay disjoint so the overlap cannot creep back.

    Excludes ``avatar`` for a different reason: image upload is the Photos
    domain's problem (size limits, downscaling, quota) and wiring a second
    upload path through here would duplicate all of it. ``avatar_url`` stays
    read-only until that service is reused here.
    """

    bio = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=MAX_PROFILE_BIO_LENGTH)
    area = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    started_exploring = serializers.DateField(required=False, allow_null=True)


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

    Driven by ``services.notifications.notification_center.preference_field_names``, which
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

    Deliberately not ``services.media.images.image_to_gallery_json``: that one takes
    an ``HttpRequest``, builds absolute URLs and template-facing flags for the
    site's own gallery, and is free to change shape whenever the frontend
    needs it. This payload is a published contract.

    Every field naming a *person* or a *space the viewer may not belong to* is
    resolved through the same gate the site's own pages use, so a photo the
    viewer can legitimately see never becomes a side channel for context they
    cannot:

    - ``owner_slug`` goes through ``services.profile.identity_visibility`` and is null
      when the uploader's privacy settings hide them from this viewer.
    - ``wiki_slug``/``wiki_name`` go through ``services.wiki.wiki_access`` and are
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
    from urbanlens.dashboard.services.memories.photos import classify_photo
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity
    from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

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


class PinSuggestionApiSerializer(serializers.Serializer):
    """One pending batch-scan pin suggestion (schema-only).

    A field-for-field mirror of ``models.pin_suggestions.model.PinSuggestion``,
    the sibling of ``VisitSuggestionSerializer`` for the other suggestion kind.
    """

    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    origin = serializers.CharField(read_only=True)
    #: True when accepting would create a brand-new pin rather than log a
    #: visit on one the profile already has.
    is_new_pin = serializers.BooleanField(read_only=True)
    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    pin_name = serializers.CharField(read_only=True, allow_null=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True)
    hit_count = serializers.IntegerField(read_only=True)
    visit_dates = serializers.ListField(child=serializers.CharField(), read_only=True)
    suggested_name = serializers.CharField(read_only=True, allow_blank=True)
    suggested_description = serializers.CharField(read_only=True, allow_blank=True)
    suggested_pin_type = serializers.CharField(read_only=True, allow_blank=True)
    suggested_aliases = serializers.ListField(child=serializers.CharField(), read_only=True)
    suggested_links = serializers.ListField(child=serializers.DictField(), read_only=True)
    created = serializers.DateTimeField(read_only=True)


class PinSuggestionListResponseSerializer(serializers.Serializer):
    """The pending pin-suggestions list envelope (schema-only)."""

    suggestions = PinSuggestionApiSerializer(many=True, read_only=True)


class MemoryEventSerializer(serializers.Serializer):
    """One row in a profile's unified Memories timeline (schema-only).

    A field-for-field mirror of ``services.memories.aggregator.MemoryEvent``.
    """

    type = serializers.CharField(read_only=True)
    occurred_at = serializers.DateTimeField(read_only=True)
    ended_at = serializers.DateTimeField(read_only=True, allow_null=True)
    title = serializers.CharField(read_only=True)
    subtitle = serializers.CharField(read_only=True, allow_blank=True)
    latitude = serializers.FloatField(read_only=True, allow_null=True)
    longitude = serializers.FloatField(read_only=True, allow_null=True)
    url = serializers.CharField(read_only=True, allow_blank=True)
    thumbnail_url = serializers.CharField(read_only=True, allow_null=True)
    icon = serializers.CharField(read_only=True)
    color = serializers.CharField(read_only=True)
    extra = serializers.JSONField(read_only=True)


class MemoriesTimelineQuerySerializer(serializers.Serializer):
    """Validates the query params of the Memories timeline endpoint."""

    start = serializers.DateField(required=False, allow_null=True, default=None)
    end = serializers.DateField(required=False, allow_null=True, default=None)
    #: "minLat,minLng,maxLat,maxLng" - silently ignored if malformed, matching
    #: the internal Memories page's own tolerant bbox parsing.
    bbox = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)


class OnThisDayVisitSerializer(serializers.Serializer):
    """One past-year visit surfaced by the on-this-day endpoint (schema-only)."""

    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    pin_name = serializers.CharField(read_only=True, allow_null=True)
    visited_at = serializers.DateTimeField(read_only=True)
    notes = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)


class OnThisDayRouteSerializer(serializers.Serializer):
    """One past-year route surfaced by the on-this-day endpoint (schema-only)."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True, allow_blank=True)
    started_at = serializers.DateTimeField(read_only=True, allow_null=True)
    distance_meters = serializers.FloatField(read_only=True)
    #: GeoJSON LineString.
    path = serializers.JSONField(read_only=True)


class OnThisDayResponseSerializer(serializers.Serializer):
    """The on-this-day recap envelope (schema-only)."""

    today = serializers.CharField(read_only=True)
    visits = OnThisDayVisitSerializer(many=True, read_only=True)
    routes = OnThisDayRouteSerializer(many=True, read_only=True)
    photos = PhotoSerializer(many=True, read_only=True)


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


class JournalResponseSerializer(serializers.Serializer):
    """One page of the Memories journal (schema-only).

    The standard ``{count, next, previous, results}`` envelope, plus one
    addition - see ``omitted_sources`` below.
    """

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = JournalEntrySerializer(many=True, read_only=True)
    #: Journal sources dropped because the credential lacks their domain scopes
    #: (``visits``, ``reviews``, ``comments``, ``articles``). Empty for a
    #: session caller or a fully scoped credential. Present so a client can
    #: distinguish an empty feed from an under-scoped one and prompt for
    #: re-authorization rather than rendering a permanently blank timeline.
    omitted_sources = serializers.ListField(child=serializers.CharField(), read_only=True)


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

    Always sourced from ``services.profile.identity_visibility.resolve_visible_identities``'
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
    ``services.trips.trip_access.can_perform``, not re-derived by the client from
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
        from urbanlens.dashboard.services.trips.trip_legs import activity_coords

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

    The map endpoint returns ``services.trips.trip_map.build_trip_map_points`` output
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
        return [{"emoji": emoji, "count": data["count"], "reacted": viewer_id in data["reacted_by"]} for emoji, data in sorted(row["reactions"].items())]

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


class _StoredRangeValidationMixin(serializers.Serializer):
    """Validates a range whose other endpoint may live on the stored row.

    A partial update is the case a plain ``attrs``-only range check cannot
    handle: when a PATCH sends one endpoint and omits the other, the omitted
    one is simply absent from ``attrs``, so ``start and end and end < start``
    is vacuously true and the check passes. Moving ``end_date`` before the
    trip's stored ``start_date``, or ``scheduled_at`` after the activity's
    stored ``scheduled_end``, was therefore accepted and persisted as an
    inverted range - which then produces incoherent itinerary and calendar
    output far from where it was introduced.

    Subclasses set :attr:`range_fields`; the view passes the row being edited
    as ``context["instance"]``. Without that context the mixin falls back to
    submitted values alone, so a caller that forgets it loses the improvement
    rather than crashing.

    A real ``Serializer`` subclass rather than a bare mixin over ``object``: it
    reads ``self.context`` and chains through ``super().validate()``, so its
    only correct use *is* as part of a serializer, and saying that in the base
    list is what lets the type checker see both members. It declares no fields,
    so it contributes nothing to ``_declared_fields`` and the concrete classes'
    own bases keep deciding the field set - inheriting it costs only the
    validation behaviour it exists for.
    """

    #: ``(start_field, end_field, message)`` for the range this serializer owns.
    range_fields: ClassVar[tuple[str, str, str]]

    def _resolve_range(self, attrs: dict) -> tuple[Any, Any]:
        """Combine submitted values with the stored instance's.

        Args:
            attrs: The validated field values for this request.

        Returns:
            The ``(start, end)`` pair the update would result in.
        """
        start_field, end_field, _message = self.range_fields
        instance = self.context.get("instance")
        start = attrs[start_field] if start_field in attrs else getattr(instance, start_field, None)
        end = attrs[end_field] if end_field in attrs else getattr(instance, end_field, None)
        return start, end

    def validate(self, attrs: dict) -> dict:
        """Reject an update that would leave the range inverted.

        Args:
            attrs: The validated field values.

        Returns:
            The unchanged values when the resulting range is coherent.

        Raises:
            serializers.ValidationError: The resulting range ends before it
                starts.
        """
        attrs = super().validate(attrs)
        _start_field, _end_field, message = self.range_fields
        start, end = self._resolve_range(attrs)
        if start and end and end < start:
            raise serializers.ValidationError(message)
        return attrs


class TripUpdateSerializer(_StoredRangeValidationMixin):
    """Validates a partial trip update.

    No field carries a default, so ``"x" in validated_data`` distinguishes
    "omitted" from "explicitly set to null" - the same presence-keyed pattern
    :class:`PinUpdateSerializer` uses, and what ``services.trips.trip_crud.update_trip``
    expects.

    Unlike :class:`TripCreateSerializer` this had no range validation at all,
    so a PATCH naming both dates inverted stored them unchecked; the mixin adds
    that plus the stored-value comparison a partial update needs.
    """

    range_fields: ClassVar[tuple[str, str, str]] = ("start_date", "end_date", "end_date cannot be before start_date.")

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


class TripActivityUpdateSerializer(_StoredRangeValidationMixin, TripActivityCreateSerializer):
    """Validates a partial activity update.

    Every field drops its default so presence drives the update, exactly as in
    :class:`TripUpdateSerializer`.

    Dropping the defaults is exactly what broke the inherited schedule check:
    with ``default=None`` gone, an omitted endpoint is absent from ``attrs``
    rather than present-and-None, so the parent's ``start and end`` guard
    silently skipped every single-endpoint PATCH. The mixin supplies the
    missing half from the stored activity - see its docstring. The parent's
    coordinate-pairing check still runs through ``super().validate``.
    """

    range_fields: ClassVar[tuple[str, str, str]] = ("scheduled_at", "scheduled_end", "scheduled_end cannot be before scheduled_at.")

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
    ``services.trips.trip_activities.complete_activity``, which also logs the
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
