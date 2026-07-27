"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from django.db import transaction
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope
from urbanlens.dashboard.external_api.serializers import (
    AuthSessionSerializer,
    ErrorSerializer,
    FriendInviteResponseSerializer,
    FriendInviteSerializer,
    FriendListQuerySerializer,
    FriendListResponseSerializer,
    FriendRequestCreateSerializer,
    FriendshipSerializer,
    JournalEntrySerializer,
    JournalQuerySerializer,
    JournalResponseSerializer,
    LabelCustomizationSerializer,
    LabelMergeResponseSerializer,
    LabelMergeSerializer,
    LabelQuerySerializer,
    LabelSerializer,
    LabelWriteSerializer,
    NotificationListQuerySerializer,
    NotificationListResponseSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
    PhotoFileSerializer,
    PhotoLabelsSerializer,
    PhotoListQuerySerializer,
    PhotoListResponseSerializer,
    PhotoSerializer,
    PhotoUploadSerializer,
    PhotoVoteResponseSerializer,
    PhotoVoteSerializer,
    PinCreateResponseSerializer,
    PinCreateSerializer,
    PinDetailSerializer,
    PinListDetailSerializer,
    PinListItemsAddResponseSerializer,
    PinListItemsDeleteSerializer,
    PinListItemSerializer,
    PinListItemsRemoveResponseSerializer,
    PinListItemsReorderResponseSerializer,
    PinListItemsReorderSerializer,
    PinListItemsWriteSerializer,
    PinListQuerySerializer,
    PinListResyncResponseSerializer,
    PinListSerializer,
    PinListWriteSerializer,
    PinSuggestionCreateResponseSerializer,
    PinSuggestionCreateSerializer,
    PinSyncQuerySerializer,
    PinSyncResponseSerializer,
    PinUpdateSerializer,
    ProfileDetailSerializer,
    ProfileNoteSerializer,
    ProfileNoteWriteSerializer,
    ProfileUpdateSerializer,
    PushDeviceRegisterSerializer,
    PushDeviceResponseSerializer,
    SavedFilterSerializer,
    SavedFilterUpdateResponseSerializer,
    SavedFilterWriteSerializer,
    SettingsPatchSerializer,
    SettingsSerializer,
    TombstoneSyncQuerySerializer,
    TombstoneSyncResponseSerializer,
    UnreadCountSerializer,
    VisitSuggestionListResponseSerializer,
    WhoAmISerializer,
    build_photo_payload,
)
from urbanlens.dashboard.external_api.throttling import (
    TIER_READ,
    ExternalApiBurstThrottle,
    ExternalApiReadThrottle,
    ExternalApiResyncThrottle,
    ExternalApiWriteThrottle,
)
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.profile.model import _COMMUNITY_GATED_VISIBILITY_FIELDS, Profile
from urbanlens.dashboard.models.profile.note import ProfileNote
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.filter_criteria import CriteriaOwnershipError, validate_criteria_ownership
from urbanlens.dashboard.services.friendship import (
    DEFAULT_FRIEND_PAGE_SIZE,
    FriendLimitExceededError,
    FriendshipActionError,
    FriendshipNotFoundError,
    InviteRateLimitedError,
    InviteValidationError,
    accept_friend_request,
    block_profile,
    ignore_friend_request,
    invite_by_email,
    list_friendships,
    mute_profile,
    reject_friend_request,
    remove_friend,
    request_or_accept_friendship,
)
from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.labels.customization import clear_label_customization, upsert_label_customization
from urbanlens.dashboard.services.labels.hierarchy import would_create_cycle
from urbanlens.dashboard.services.labels.merge import LabelMergeError, merge_labels
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.media_labels import MediaLabelError, set_media_labels
from urbanlens.dashboard.services.media_relevance import toggle_media_vote
from urbanlens.dashboard.services.memories.journal import get_journal_entries
from urbanlens.dashboard.services.memories.photos import create_pin_and_log_visit, log_visit_on_pin
from urbanlens.dashboard.services.notification_center import (
    DEFAULT_NOTIFICATION_PAGE_SIZE,
    InvalidNotificationCursorError,
    get_preferences,
    list_notifications,
    mark_all_read,
    mark_notification_read,
    serialize_preferences,
    unread_count,
    update_preferences,
)
from urbanlens.dashboard.services.photo_upload import PhotoUploadError, upload_photo
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, PinReparentError, delete_pin, move_pin_to_coordinates, reparent_pin
from urbanlens.dashboard.services.pin_list_membership import (
    add_pins_to_list,
    remove_pins_from_list,
    reorder_list_items,
    resync_lists_for_saved_filter,
    resync_smart_list,
)
from urbanlens.dashboard.services.pin_suggestions import LocationHit, attach_suggestion_photos, ingest_location_hits
from urbanlens.dashboard.services.pin_sync import InvalidSyncCursorError, StaleDeletedSinceError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.profile_settings import SettingsValidationError, apply_settings_patch, read_settings
from urbanlens.dashboard.services.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.visits import accept_visit_suggestion, reject_visit_suggestion, visit_logging_allowed

if TYPE_CHECKING:
    from collections.abc import Callable

    from rest_framework.request import Request

logger = logging.getLogger(__name__)

#: Fixed source_key for the single hit a pin-suggestion POST produces - this
#: endpoint is one discovered place per call (mirrors PinsView.post), so there's
#: never more than one id to look up in IngestSummary.suggestion_ids_by_key.
_SUGGESTION_SOURCE_KEY = "external_api_submission"


def _get_pin_list(request: Request, list_slug: str) -> PinList | None:
    """The caller's pin list matching *list_slug* (by slug or uuid), or None.

    Another profile's list reads as "not found" rather than "forbidden" - the
    existence of someone else's list is not the caller's business.

    Args:
        request: The authenticated request.
        list_slug: The list's slug, or its uuid as a string.

    Returns:
        The matching list, or None.
    """
    queryset = PinList.objects.for_profile(request.user.profile).select_related("source_saved_filter")
    pin_list = queryset.filter(slug=list_slug).first()
    if pin_list is not None:
        return pin_list
    try:
        parsed = UUID(list_slug)
    except (ValueError, AttributeError, TypeError):
        return None
    return queryset.filter(uuid=parsed).first()


def _get_label(request: Request, label_uuid: UUID) -> Label | None:
    """A label visible to the caller, with their customizations prefetched.

    The ``with_customizations_for`` call is required for the ``effective_*``
    fields to be correct rather than silently wrong - see
    :class:`LabelsView`'s docstring.

    Args:
        request: The authenticated request.
        label_uuid: The label's uuid.

    Returns:
        The matching label, or None.
    """
    profile = request.user.profile
    return Label.objects.visible_to(profile).with_customizations_for(profile).prefetch_related("parents").filter(uuid=label_uuid).first()


def _reload_label(label: Label, profile: Profile) -> Label:
    """Re-read *label* with customizations prefetched, for a post-write response.

    A label that was just created or mutated in memory has no
    ``_user_customizations`` attribute (or a stale one), which would make every
    ``effective_*`` field in the response wrong. Re-reading is the only way to
    populate it, since the prefetch is a queryset-level operation.

    Args:
        label: The label just written.
        profile: The caller, whose customizations to load.

    Returns:
        The freshly-loaded label. Falls back to the in-memory instance if the
        row has vanished (it was just deleted by a concurrent request).
    """
    reloaded = Label.objects.visible_to(profile).with_customizations_for(profile).prefetch_related("parents").filter(pk=label.pk).first()
    return reloaded if reloaded is not None else label


def _refuse_label_write(label: Label) -> Response | None:
    """Refuse a write to a global or protected label.

    Args:
        label: The label being written to.

    Returns:
        A 403 response when the write must be refused, otherwise None.
    """
    if label.profile_id is None:
        return Response({"error": "Global labels cannot be modified. Use the customization endpoint instead."}, status=403)
    if label.is_protected:
        return Response({"error": "This label is protected and cannot be modified."}, status=403)
    return None


def _resolve_source_saved_filter(profile: Profile, data: dict) -> tuple[SavedFilter | None, Response | None]:
    """Resolve a submitted ``source_saved_filter_uuid`` to one of *profile*'s filters.

    Args:
        profile: The owner the filter must belong to.
        data: Validated write-serializer data.

    Returns:
        ``(saved_filter, None)`` on success - where ``saved_filter`` is None
        when the key was absent or explicitly null - or ``(None, response)``
        carrying a 400 when the uuid names no filter of the caller's.
    """
    if not data.get("source_saved_filter_uuid"):
        return None, None
    saved_filter = SavedFilter.objects.filter(uuid=data["source_saved_filter_uuid"], profile=profile).first()
    if saved_filter is None:
        return None, Response({"error": "No such saved filter."}, status=400)
    return saved_filter, None


def _resolve_parent_labels(profile: Profile, data: dict) -> tuple[list[Label], Response | None]:
    """Resolve submitted ``parent_uuids`` to labels visible to *profile*.

    Args:
        profile: The caller.
        data: Validated write-serializer data.

    Returns:
        ``(parents, None)`` on success, or ``(([], response)`` carrying a 400
        when any uuid names no visible label. Unknown parents are rejected
        rather than dropped: silently building a different hierarchy than the
        client asked for is worse than refusing.
    """
    if "parent_uuids" not in data:
        return [], None
    uuids = data["parent_uuids"]
    parents = list(Label.objects.visible_to(profile).filter(uuid__in=uuids))
    if len(parents) != len(set(uuids)):
        return [], Response({"error": "One or more parent labels do not exist."}, status=400)
    return parents, None


class ExternalApiView(APIView):
    """Base for every external endpoint: credential auth, scope gate, per-credential throttle.

    Two credential kinds are accepted - PAT-style ``ApiKey`` bearer keys and
    django-oauth-toolkit access tokens (the native apps' OAuth2 + PKCE flow) -
    both enforced against the same per-method scope declarations.

    Scopes are declared per HTTP method in ``required_scopes_by_method``;
    ``HasApiKeyScope`` reads the ``required_scopes`` property and fails closed
    when the current method has no entry, so an endpoint can never gain a new
    method without also declaring what that method requires.
    """

    authentication_classes = [ApiKeyAuthentication, OAuth2Authentication]
    permission_classes = [HasApiKeyScope]
    #: All three apply together: the burst cap counts every request, while the
    #: read and write caps each count only their own tier (see ``throttling``).
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {}

    @property
    def required_scopes(self) -> frozenset[ApiKeyScope]:
        """The scopes the current request's HTTP method requires."""
        return self.required_scopes_by_method.get(self.request.method or "", frozenset())


class UnscopedExternalApiView(ExternalApiView):
    """Base for the rare endpoint that needs authentication but no particular scope.

    The one deliberate exception to ``HasApiKeyScope``'s fail-closed default,
    and reserved for endpoints that describe *the credential itself* rather
    than any of the user's data. Requiring a scope there would be circular: a
    client asks what it may do precisely because it doesn't yet know, and a
    credential can always be told its own shape without that revealing anything
    it couldn't already discover by probing.

    Do not use this as a shortcut for an endpoint that touches user data - such
    an endpoint needs a scope, and inheriting from here would silently grant it
    to every credential.
    """

    permission_classes = [IsAuthenticated]


class WhoAmIView(ExternalApiView):
    """GET: the calling API key's owner - just their uuid, nothing else.

    The only *profile* data an external application can read: no settings,
    friends, or any other private data, per the ``profile:read`` scope's
    definition.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PROFILE_READ}),
    }

    @extend_schema(responses=WhoAmISerializer)
    def get(self, request: Request) -> Response:
        """Return the authenticated key owner's profile uuid."""
        profile = request.user.profile
        return Response(WhoAmISerializer(profile).data)


class PinsView(ExternalApiView):
    """The key owner's pins: GET delta-syncs them, POST creates one.

    GET is a sync feed, not a browse API: ordered by ``(updated, pk)``, it
    pages through pins changed since ``modified_since`` with an opaque cursor
    and hands back the ``sync_watermark`` to use as the next sync's
    ``modified_since``. Deletions are the separate ``pins/deleted/`` feed.

    POST goes through the exact same ``services.pin_creation.create_pin_for_profile``
    call as the map UI's "Add pin" form - the same sanitization, geocoding
    gate, and background enrichment apply regardless of which caller created
    the pin. A caller-generated ``uuid`` makes the create idempotent for
    offline-outbox retries.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(parameters=[PinSyncQuerySerializer], responses={200: PinSyncResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the key owner's pins changed since ``modified_since``."""
        serializer = PinSyncQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        try:
            page = sync_pins_page(
                request.user.profile,
                modified_since=params.get("modified_since"),
                cursor=params.get("cursor") or None,
                limit=params.get("limit"),
                include_total=params.get("include_total", False),
            )
        except InvalidSyncCursorError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(
            {
                "pins": page.pins,
                "next_cursor": page.next_cursor,
                "sync_watermark": page.sync_watermark,
                "total": page.total,
            }
        )

    @extend_schema(
        request=PinCreateSerializer,
        responses={201: PinCreateResponseSerializer, 200: PinCreateResponseSerializer, 400: ErrorSerializer, 403: ErrorSerializer},
    )
    def post(self, request: Request) -> Response:
        """Validate the payload and create a pin owned by the key's user."""
        serializer = PinCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = create_pin_for_profile(
                request.user.profile,
                name=data.get("name"),
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                address=data.get("address"),
                icon=data.get("icon"),
                color=data.get("color"),
                description=data.get("description"),
                pin_type=data.get("pin_type"),
                client_uuid=data.get("uuid"),
            )
        except PinCreationForbiddenError as exc:
            return Response({"error": str(exc)}, status=403)
        except PinCreationError as exc:
            return Response({"error": str(exc)}, status=400)

        pin = result.pin
        return Response(
            {
                "uuid": str(pin.uuid),
                "slug": pin.slug,
                "name": pin.effective_name,
                # True when the coordinates also match another existing Location -
                # the pin was still created, but callers may want to flag this for
                # manual review rather than silently trusting the auto-resolved place.
                "ambiguous_location": len(result.all_locations) > 1,
                # False when this was an idempotent replay of an earlier create
                # (same client-generated uuid) - the pin already existed.
                "created": result.created,
            },
            status=201 if result.created else 200,
        )


class PinDetailView(ExternalApiView):
    """GET the key owner's full pin detail; PATCH or DELETE it.

    GET returns a superset of the sync feed's payload - description, dates,
    security indicators, personal notes/aliases/links, custom fields, the
    property boundary, the cover photo, and the discovered wiki slug (see
    ``services.pin_detail.build_pin_detail``).

    PATCH extends the same semantics as the internal ``PinViewSet``
    (renaming, re-icon, a coordinate move that relinks the Location) plus
    ``parent_id`` to detach (``null``) or re-parent a pin under another of
    the caller's own pins - something no single internal endpoint exposes.

    DELETE mirrors ``PinViewSet.destroy``: a pin with child pins requires an
    explicit ``?children=delete`` or ``?children=keep``, refused with 409
    otherwise; every deletion stages an Undo History entry and writes a
    tombstone for sync clients.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "PATCH": frozenset({ApiKeyScope.PINS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    def _get_pin(self, request: Request, pin_slug: str) -> Pin | None:
        """The key owner's pin matching *pin_slug* (by slug or uuid), or None."""
        return Pin.objects.slug_or_uuid(pin_slug).filter(profile__user=request.user).select_related("location", "profile", "parent_pin", "wiki", "cover_photo").first()

    @extend_schema(responses={200: PinDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return the key owner's full detail for one pin."""
        pin = self._get_pin(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)
        return Response(build_pin_detail(pin, request.user.profile))

    @extend_schema(request=PinUpdateSerializer, responses={200: PinDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, pin_slug: str) -> Response:
        """Apply a partial update to one of the key owner's pins."""
        pin = self._get_pin(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        serializer = PinUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Resolved before the transaction below so an unknown parent uuid never
        # needs a rollback - nothing has been written yet at this point.
        new_parent = None
        if "parent_id" in data and data["parent_id"] is not None:
            new_parent = Pin.objects.filter(uuid=data["parent_id"], profile=pin.profile).first()
            if new_parent is None:
                return Response({"error": "No such pin to set as parent."}, status=400)

        try:
            with transaction.atomic():
                if "latitude" in data:
                    move_pin_to_coordinates(pin, data["latitude"], data["longitude"])

                update_fields: list[str] = []
                if "name" in data:
                    pin.name = (data["name"] or "").strip() or None
                    pin.name_is_user_provided = bool(pin.name)
                    update_fields += ["name", "name_is_user_provided"]
                if "icon" in data:
                    pin.icon = data["icon"] or None
                    update_fields.append("icon")
                if "last_visited" in data:
                    pin.last_visited = data["last_visited"]
                    update_fields.append("last_visited")
                if update_fields:
                    pin.save(update_fields=[*update_fields, "updated"])

                if "parent_id" in data:
                    # Raises on failure - propagating out of the atomic block rolls
                    # back any coordinate/name/icon change already applied above.
                    reparent_pin(pin, new_parent)
        except PinReparentError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(build_pin_detail(pin, request.user.profile))

    @extend_schema(responses={204: None, 404: ErrorSerializer, 409: ErrorSerializer})
    def delete(self, request: Request, pin_slug: str) -> Response:
        """Delete one of the key owner's pins, per ``PinViewSet.destroy`` semantics."""
        pin = self._get_pin(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        children_mode = (request.query_params.get("children") or "").strip().lower()
        try:
            delete_pin(pin, children_mode=children_mode)
        except PinHasChildrenError as exc:
            return Response(
                {"error": "This pin has child pins - resend with ?children=delete or ?children=keep.", "requires_children_decision": True, "children": exc.descendant_count},
                status=409,
            )
        return Response(status=204)


class PinSuggestionsView(ExternalApiView):
    """POST: submit a discovered place as a pending suggestion, not a real pin.

    Unlike ``PinsView.post``, nothing is created on the map immediately - the
    submission is staged as a ``PinSuggestion`` (see
    ``services.pin_suggestions.ingest_location_hits``) that the key's owner
    must explicitly accept or reject from the Memories -> Locations review
    queue before anything appears. An external "discovery" app that finds
    candidate places autonomously (rather than acting on the user's own
    behalf, like the mobile app's offline outbox does for ``PinsView``)
    should use this endpoint instead.

    A submission near one of the owner's existing pins, or another pending
    suggestion, merges into it exactly like an Immich/local-scan hit would -
    this is the same clustering/matching pipeline, just a third kind of hit.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(
        request=PinSuggestionCreateSerializer,
        responses={201: PinSuggestionCreateResponseSerializer, 400: ErrorSerializer, 403: ErrorSerializer},
    )
    def post(self, request: Request) -> Response:
        """Validate the payload and stage a pending PinSuggestion for the key's user."""
        serializer = PinSuggestionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        # A PinSuggestion is itself a location-history trail (see
        # ingest_location_hits) - fail closed exactly like the local-scan
        # upload endpoint does, rather than silently accepting and dropping it.
        if not visit_logging_allowed(profile):
            return Response({"error": "Visit-history tracking is turned off in your settings."}, status=403)

        latitude = data.get("latitude")
        longitude = data.get("longitude")
        address = data.get("address")
        if latitude is None or longitude is None:
            if not profile.external_apis_enabled:
                return Response({"error": "External lookups are turned off in your settings - drop a pin on the map instead."}, status=403)
            latitude, longitude = get_pin_by_address(address)
            if latitude is None or longitude is None:
                return Response({"error": "Unable to convert address to lat/lng."}, status=400)

        hit = LocationHit(
            latitude=float(latitude),
            longitude=float(longitude),
            # Never surfaced: _dates_from_hits skips non-visit hits entirely.
            taken_at=timezone.now(),
            label=data.get("name") or None,
            source_key=_SUGGESTION_SOURCE_KEY,
            description=data.get("description") or None,
            pin_type=data.get("pin_type") or None,
            aliases=tuple(data.get("aliases") or ()),
            links=tuple((link["name"], link["url"]) for link in data.get("links") or ()),
            implies_visit=False,
        )
        summary = ingest_location_hits(profile, [hit], origin=PinSuggestionOrigin.EXTERNAL_API)
        suggestion = PinSuggestion.objects.get(pk=summary.suggestion_ids_by_key[_SUGGESTION_SOURCE_KEY])
        photos = data.get("photos") or []
        attached = attach_suggestion_photos(suggestion, photos, profile) if photos else []

        return Response(
            {
                "suggestion_id": suggestion.pk,
                "status": suggestion.status,
                "matched_existing_pin": not suggestion.is_new_pin,
                "photos_attached": len(attached),
                "review_url": reverse("memories.locations"),
            },
            status=201,
        )


class PinTombstonesView(ExternalApiView):
    """GET: the key owner's pin deletions since ``deleted_since``, for delta sync.

    Serves ``PinTombstone`` rows - the durable record written when a pin is
    hard-deleted. Without this feed a sync client can learn about new and
    changed pins from ``pins/`` but would hold deleted ones forever.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
    }

    @extend_schema(parameters=[TombstoneSyncQuerySerializer], responses={200: TombstoneSyncResponseSerializer, 400: ErrorSerializer, 410: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the key owner's pin deletions."""
        serializer = TombstoneSyncQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        try:
            page = sync_tombstones_page(
                request.user.profile,
                deleted_since=params.get("deleted_since"),
                cursor=params.get("cursor") or None,
                limit=params.get("limit"),
            )
        except InvalidSyncCursorError as exc:
            return Response({"error": str(exc)}, status=400)
        except StaleDeletedSinceError as exc:
            # 410 Gone: tombstones this old may already be pruned, so the
            # incremental deletions feed can no longer be trusted from that
            # point. The client must full-resync (walk pins/ without
            # modified_since and drop local pins absent from the result).
            return Response({"error": str(exc), "full_resync_required": True}, status=410)

        return Response(
            {
                "tombstones": page.tombstones,
                "next_cursor": page.next_cursor,
                "sync_watermark": page.sync_watermark,
            }
        )


class PushDevicesView(ExternalApiView):
    """POST: register (or re-activate) this device as a push destination.

    Idempotent on the submitted address, so an app can re-register on every
    launch without tracking whether it already did. The response echoes the
    device's public ``uuid``, which is what ``DELETE push-devices/<uuid>/``
    takes to unregister.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PUSH_MANAGE}),
    }

    @extend_schema(request=PushDeviceRegisterSerializer, responses={201: PushDeviceResponseSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Validate and register the submitted push destination."""
        serializer = PushDeviceRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            device = register_device(
                request.user.profile,
                transport=data["transport"],
                address=data["address"],
                name=data.get("name", ""),
            )
        except PushRegistrationError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(PushDeviceResponseSerializer(device).data, status=201)


class AccountSettingsView(ExternalApiView):
    """GET the caller's account preferences; PATCH to change them.

    Named for the account rather than matching ``controllers.settings.SettingsView``
    (the site's own multi-form settings page) - the two are unrelated and share
    only the underlying ``Profile`` fields, via
    ``services.profile_settings``.

    PATCH is partial by construction: only submitted keys are touched, so a
    client syncing one toggle never overwrites preferences changed on the web
    in the meantime. The response is always the full post-save document, since
    ``Profile.save()`` may coerce community-gated fields and the client needs
    to see what it actually ended up with.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SETTINGS_READ}),
        "PATCH": frozenset({ApiKeyScope.SETTINGS_WRITE}),
    }

    @extend_schema(responses={200: SettingsSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's full settings document."""
        return Response(SettingsSerializer(read_settings(request.user.profile, user=request.user)).data)

    @extend_schema(request=SettingsPatchSerializer, responses={200: SettingsSerializer, 400: ErrorSerializer})
    def patch(self, request: Request) -> Response:
        """Apply a partial settings update and return the resulting document."""
        serializer = SettingsPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile

        try:
            touched = apply_settings_patch(profile, serializer.validated_data, user=request.user)
        except SettingsValidationError as exc:
            # Per-field, unlike the internal view's silent no-op: a sync client
            # that cannot tell a rejected write from an accepted one will retry
            # it forever.
            return Response({"error": "Some settings could not be changed.", "fields": exc.errors}, status=400)

        if touched:
            with transaction.atomic():
                profile.save(update_fields=[*touched, "updated"])

        # Re-read from the saved instance rather than echoing the submission:
        # Profile.save() forces the community-gated visibility and wiki-sync
        # fields off when community_enabled is False, and the client must be
        # told the coerced values, not the ones it asked for.
        return Response(SettingsSerializer(read_settings(profile, user=request.user)).data)


class AuthSessionView(UnscopedExternalApiView):
    """GET: what the calling credential is and what it may do.

    Deliberately scope-free (see :class:`UnscopedExternalApiView`) - a client
    calls this to discover its own grant, so gating it behind a scope would be
    circular. It reveals nothing the caller couldn't establish by probing
    endpoints and collecting 403s; it just saves it the trouble, and lets it
    schedule a token refresh before ``expires_at`` instead of after a failure.
    """

    #: This is a read despite declaring no scopes, which request_tier would
    #: otherwise conservatively classify as a write.
    throttle_tier_by_method: ClassVar[dict[str, str]] = {"GET": TIER_READ}

    @extend_schema(responses={200: AuthSessionSerializer})
    def get(self, request: Request) -> Response:
        """Describe the credential this request authenticated with."""
        credential = request.auth
        # Same discriminator permissions.py uses: only an OAuth2 AccessToken
        # carries allow_scopes. Its `scopes` attribute is a {name: description}
        # dict, so the granted list comes from the raw `scope` string instead.
        if hasattr(credential, "allow_scopes"):
            application = credential.application
            payload = {
                "credential_type": "oauth2",
                "scopes": sorted(credential.scope.split()),
                "expires_at": credential.expires,
                "issued_at": credential.created,
                "client_id": application.client_id if application else None,
                "name": application.name if application else None,
            }
        else:
            payload = {
                "credential_type": "api_key",
                "scopes": sorted(credential.scopes or []),
                # API keys do not expire - they are revoked, and a revoked one
                # never authenticates in the first place.
                "expires_at": None,
                "issued_at": credential.created,
                "client_id": None,
                "name": credential.name,
            }
        payload["user_uuid"] = request.user.profile.uuid
        return Response(AuthSessionSerializer(payload).data)


class _OwnedImageMixin:
    """Resolves the ``<uuid:image_uuid>`` path segment to a photo the caller owns.

    Scoped to ``profile__user=request.user`` and deliberately **not** to
    ``Image.objects.visible_to(...)``: visibility is a *read* relation that
    includes friends' and community photos, and every write endpoint here
    (delete, relabel, re-file, vote) would otherwise let a caller mutate a
    photo they merely happen to be allowed to look at. The one read endpoint
    that may widen to ``visible_to`` does so explicitly, on its own.
    """

    def _get_image(self, request: Request, image_uuid: UUID) -> Image | None:
        """Return the caller's own photo with this uuid, or None."""
        return (
            Image.objects.filter(uuid=image_uuid, profile__user=request.user)
            .select_related("pin", "wiki", "wiki__location", "visit", "location", "profile", "direct_message")
            .prefetch_related("labels")
            .first()
        )


def _resolve_own_pin(request: Request, value: str) -> Pin | None:
    """Resolve a pin slug-or-uuid against the caller's own pins only."""
    return Pin.objects.slug_or_uuid(value).filter(profile__user=request.user).select_related("location").first()


class PhotosView(PaginatedListMixin, ExternalApiView):
    """The key owner's photo library: GET browses it, POST uploads to it.

    GET is a browse endpoint (page-number paginated), not a delta sync: unlike
    ``pins/`` there is no tombstone feed for photos, so a client that needs to
    detect deletions re-walks the list.

    POST runs the identical admission pipeline as the Memories page's
    drag-and-drop uploader (``services.photo_upload.upload_photo``) - the same
    media-type sniffing, feature gates, malware/size checks, duplicate
    rejection and storage quota.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }
    parser_classes = [MultiPartParser]

    @extend_schema(parameters=[PhotoListQuerySerializer], responses={200: PhotoListResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the key owner's own photos."""
        serializer = PhotoListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        profile = request.user.profile

        queryset = Image.objects.uploaded_by(profile).select_related("pin", "wiki", "wiki__location", "visit", "location", "profile", "direct_message")
        # Without this the payload builder issues a labels query per row.
        queryset = queryset.prefetch_related("labels")

        pin_ref = (params.get("pin") or "").strip()
        if pin_ref:
            pin = _resolve_own_pin(request, pin_ref)
            if pin is None:
                return Response({"error": "No such pin."}, status=400)
            queryset = queryset.filter(pin=pin)

        if params.get("unfiled"):
            queryset = queryset.filter(pin__isnull=True, visit__isnull=True)

        if params.get("media_type"):
            queryset = queryset.filter(media_type=params["media_type"])

        taken_from = params.get("taken_from")
        taken_to = params.get("taken_to")
        if taken_from is not None or taken_to is not None:
            # taken_at is null for anything without EXIF, so the upload time
            # stands in - matching how the rest of the app orders photos.
            queryset = queryset.annotate(_taken=Coalesce("taken_at", "created"))
            if taken_from is not None:
                queryset = queryset.filter(_taken__gte=taken_from)
            if taken_to is not None:
                queryset = queryset.filter(_taken__lte=taken_to)

        # pk breaks ties so pages can't overlap when many rows share a
        # created timestamp (a bulk import writes dozens in the same instant).
        queryset = queryset.order_by("-created", "-pk")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        payload = [build_photo_payload(image, profile) for image in page or []]
        return paginator.get_paginated_response(PhotoSerializer(payload, many=True).data)

    @extend_schema(request=PhotoUploadSerializer, responses={201: PhotoSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 409: ErrorSerializer, 413: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Upload one photo, video, or document to the key owner's library.

        EXIF-derived fields (``latitude``/``longitude``/``taken_at``/
        ``author``) are extracted asynchronously by ``process_image_upload``
        and are normally still null in this response - re-fetch the photo
        shortly afterwards rather than treating this payload as final.
        """
        serializer = PhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        pin = None
        pin_ref = (data.get("pin") or "").strip()
        if pin_ref:
            pin = _resolve_own_pin(request, pin_ref)
            if pin is None:
                return Response({"error": "No such pin."}, status=400)

        visit = None
        if data.get("visit") is not None:
            visit = PinVisit.objects.filter(pk=data["visit"], pin__profile=profile).first()
            if visit is None:
                return Response({"error": "No such visit."}, status=400)

        try:
            image = upload_photo(profile, data["file"], caption=data.get("caption") or None, pin=pin, visit=visit)
        except PhotoUploadError as exc:
            return Response({"error": exc.message}, status=exc.status)

        return Response(PhotoSerializer(build_photo_payload(image, profile)).data, status=201)


class PhotoDetailView(_OwnedImageMixin, ExternalApiView):
    """GET one photo's metadata; DELETE it and its stored file.

    GET widens to ``Image.objects.visible_to`` when the photo isn't the
    caller's own, so a client can resolve a photo it legitimately sees in a
    shared gallery. DELETE never does - see :class:`_OwnedImageMixin`.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
        "DELETE": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    @extend_schema(responses={200: PhotoSerializer, 404: ErrorSerializer})
    def get(self, request: Request, image_uuid: UUID) -> Response:
        """Return one photo the caller owns or may see."""
        profile = request.user.profile
        image = self._get_image(request, image_uuid)
        if image is None:
            image = (
                Image.objects.visible_to(profile)
                .filter(uuid=image_uuid)
                .select_related("pin", "wiki", "wiki__location", "visit", "location", "profile", "direct_message")
                .prefetch_related("labels")
                .first()
            )
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        return Response(PhotoSerializer(build_photo_payload(image, profile)).data)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, image_uuid: UUID) -> Response:
        """Delete one of the caller's own photos, file included."""
        image = self._get_image(request, image_uuid)
        if image is None:
            # 404 rather than 403 for someone else's photo - the same
            # no-oracle policy the rest of this API and the media gate follow.
            return Response({"error": "No such photo."}, status=404)
        # Matches controllers.photos.PhotoActionView.delete_photo: drop the
        # stored file before the row, so deleting the row can't orphan bytes.
        image.image.delete(save=False)
        image.delete()
        return Response(status=204)


class PhotoLabelsView(_OwnedImageMixin, ExternalApiView):
    """PUT: replace the media labels on one of the caller's photos."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    @extend_schema(request=PhotoLabelsSerializer, responses={200: PhotoSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, image_uuid: UUID) -> Response:
        """Set the photo's labels to exactly the submitted names."""
        image = self._get_image(request, image_uuid)
        if image is None:
            return Response({"error": "No such photo."}, status=404)

        serializer = PhotoLabelsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile

        try:
            set_media_labels(image, serializer.validated_data["labels"], profile)
        except MediaLabelError as exc:
            return Response({"error": str(exc)}, status=400)

        image.refresh_from_db()
        return Response(PhotoSerializer(build_photo_payload(image, profile)).data)


class PhotoVoteView(_OwnedImageMixin, ExternalApiView):
    """POST: cast, flip, or withdraw a community relevance vote on a photo.

    Only meaningful for a photo materialized into a Location's Media gallery -
    a plain personal upload has no ``(source, item_key)`` identity for
    ``MediaRelevance`` to key a vote by, and is refused with 400.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    @extend_schema(request=PhotoVoteSerializer, responses={200: PhotoVoteResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, image_uuid: UUID) -> Response:
        """Record the caller's vote and return the item's new net score."""
        image = self._get_image(request, image_uuid)
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        if image.location_id is None or not image.media_source_key or not image.media_item_key:
            return Response({"error": "This photo is not part of a location's media gallery, so it cannot be voted on."}, status=400)

        serializer = PhotoVoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        value = serializer.validated_data["value"]

        score = toggle_media_vote(image, request.user.profile, value=value)
        return Response({"score": score, "your_vote": value})


class PhotoFileView(_OwnedImageMixin, ExternalApiView):
    """POST: file an unfiled photo onto a pin, creating one if needed.

    The API counterpart of the Memories organize queue's "log visit" and
    "create pin" actions, going through the same
    ``services.memories.photos`` functions so a photo filed here lands in the
    user's visit history identically to one filed on the site.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    @extend_schema(request=PhotoFileSerializer, responses={200: PhotoSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, image_uuid: UUID) -> Response:
        """File the photo onto an existing pin, or onto a newly created one."""
        image = self._get_image(request, image_uuid)
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        if image.pin_id:
            return Response({"error": "This photo has already been filed."}, status=409)

        serializer = PhotoFileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        # Filing a photo writes a PinVisit - the same location-history trail
        # PinSuggestionsView refuses to build when tracking is off.
        if not visit_logging_allowed(profile):
            return Response({"error": "Visit-history tracking is turned off in your settings."}, status=403)

        pin_ref = (data.get("pin") or "").strip()
        if pin_ref:
            pin = _resolve_own_pin(request, pin_ref)
            if pin is None:
                return Response({"error": "No such pin."}, status=400)
            log_visit_on_pin(profile, image, pin)
        else:
            latitude = data.get("latitude")
            longitude = data.get("longitude")
            if latitude is None or longitude is None:
                latitude = image.effective_latitude
                longitude = image.effective_longitude
            if latitude is None or longitude is None:
                return Response({"error": "This photo has no location - supply latitude and longitude, or a pin."}, status=400)
            create_pin_and_log_visit(profile, image, latitude=latitude, longitude=longitude, name=data.get("name") or None)

        image.refresh_from_db()
        return Response(PhotoSerializer(build_photo_payload(image, profile)).data)


class VisitSuggestionsView(ExternalApiView):
    """GET: the caller's pending photo-derived visit suggestions."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(responses={200: VisitSuggestionListResponseSerializer})
    def get(self, request: Request) -> Response:
        """List every pending suggestion raised from one of the caller's photos."""
        profile = request.user.profile
        suggestions = list(
            VisitSuggestion.objects.filter(origin_image__profile=profile, status=VisitSuggestionStatus.PENDING)
            .select_related("location", "origin_image", "origin_image__pin", "origin_image__wiki", "origin_image__wiki__location", "origin_image__visit", "origin_image__location", "origin_image__profile", "origin_image__direct_message")
            .prefetch_related("origin_image__labels")
            .order_by("-created", "-pk")
        )

        # One query for every suggestion's pin instead of one per row.
        location_ids = {s.location_id for s in suggestions if s.location_id}
        pins_by_location = {pin.location_id: pin for pin in Pin.objects.filter(profile=profile, location_id__in=location_ids).select_related("location")} if location_ids else {}

        payload = []
        for suggestion in suggestions:
            pin = pins_by_location.get(suggestion.location_id)
            payload.append(
                {
                    "id": suggestion.pk,
                    "status": suggestion.status,
                    "photo": build_photo_payload(suggestion.origin_image, profile),
                    "pin_slug": pin.slug if pin is not None else None,
                    "pin_name": pin.effective_name if pin is not None else None,
                    "suggested_at": suggestion.created,
                    "visit_date": suggestion.visited_at,
                }
            )
        return Response(VisitSuggestionListResponseSerializer({"suggestions": payload}).data)


class VisitSuggestionActionView(ExternalApiView):
    """POST: accept or dismiss one pending visit suggestion."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    # request=None: the action is carried entirely by the URL, so there is no
    # body for drf-spectacular to infer a request serializer from.
    @extend_schema(request=None, responses={204: None, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, suggestion_id: int, action: str) -> Response:
        """Apply ``accept`` or ``dismiss`` to one of the caller's pending suggestions."""
        if action not in {"accept", "dismiss"}:
            return Response({"error": "No such action."}, status=404)

        profile = request.user.profile
        suggestion = VisitSuggestion.objects.filter(
            pk=suggestion_id,
            origin_image__profile=profile,
            status=VisitSuggestionStatus.PENDING,
        ).first()
        if suggestion is None:
            return Response({"error": "No such suggestion."}, status=404)

        if action == "accept":
            if accept_visit_suggestion(suggestion, profile) is None:
                return Response({"error": "Visit logging is turned off."}, status=403)
        else:
            reject_visit_suggestion(suggestion)
        return Response(status=204)


class MemoriesJournalView(ExternalApiView):
    """GET: the caller's Memories journal - visit notes, ratings, comments, article edits."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(parameters=[JournalQuerySerializer], responses={200: JournalResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return one window of the caller's journal, newest first."""
        serializer = JournalQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        # get_journal_entries materializes every source in full - that is the
        # existing internal behavior (the Memories page renders the whole
        # feed), so the window is applied in Python rather than pushed into
        # the service, which would mean paginating four heterogeneous
        # querysets and merging them.
        entries = get_journal_entries(request.user.profile)
        offset = params["offset"]
        window = entries[offset : offset + params["limit"]]
        return Response(
            {
                "entries": JournalEntrySerializer(window, many=True).data,
                "total": len(entries),
            }
        )


class PinListsView(PaginatedListMixin, ExternalApiView):
    """The caller's pin lists: GET pages through them, POST creates one.

    Supports ``?is_smart=true|false`` to page through only smart or only plain
    lists.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LISTS_READ}),
        "POST": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    @extend_schema(parameters=[PinListQuerySerializer], responses={200: PinListSerializer(many=True)})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's pin lists."""
        serializer = PinListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        # prefetch_related("items") is what makes PinList.pin_count free - the
        # property counts the prefetched rows instead of issuing a COUNT per
        # list (see PinList.pin_count).
        queryset = PinList.objects.for_profile(request.user.profile).select_related("source_saved_filter").prefetch_related("items").order_by("-updated", "pk")
        if (is_smart := serializer.validated_data.get("is_smart")) is not None:
            queryset = queryset.filter(is_smart=is_smart)
        return self.paginated_response(queryset, PinListSerializer, request)

    @extend_schema(request=PinListWriteSerializer, responses={201: PinListDetailSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Create a pin list owned by the caller."""
        serializer = PinListWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        source_filter, error = _resolve_source_saved_filter(profile, data)
        if error is not None:
            return error

        smart_filter = data.get("smart_filter")
        if source_filter is not None:
            smart_filter = source_filter.criteria
        if smart_filter is not None:
            try:
                validate_criteria_ownership(smart_filter, profile)
            except CriteriaOwnershipError as exc:
                return Response({"error": str(exc)}, status=400)

        if PinList.objects.for_profile(profile).filter(name=data["name"]).exists():
            return Response({"error": "You already have a list with that name."}, status=400)

        pin_list = PinList(
            profile=profile,
            name=data["name"],
            description=data.get("description", ""),
            is_smart=data.get("is_smart", False),
            smart_filter=smart_filter,
            smart_boundary=data.get("smart_boundary"),
            source_saved_filter=source_filter,
        )
        pin_list.save()

        # A list created with rules should show its matching pins immediately,
        # not only after the next pin edit triggers the signal.
        if pin_list.smart_filter or pin_list.smart_boundary:
            resync_smart_list(pin_list)

        return Response(PinListDetailSerializer(pin_list).data, status=201)


class PinListDetailView(ExternalApiView):
    """One of the caller's pin lists: GET it, PATCH it, or DELETE it.

    PATCH recomputes membership only when the rules actually changed
    (``is_smart``, ``smart_filter``, or ``smart_boundary``) - a resync is a
    full re-evaluation of every pin the profile owns, far too expensive to run
    on an unrelated rename.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LISTS_READ}),
        "PATCH": frozenset({ApiKeyScope.LISTS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    @extend_schema(responses={200: PinListDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, list_slug: str) -> Response:
        """Return one of the caller's lists, including its boundary geometry."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)
        return Response(PinListDetailSerializer(pin_list).data)

    @extend_schema(request=PinListWriteSerializer, responses={200: PinListDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, list_slug: str) -> Response:
        """Apply a partial update, resyncing membership if the smart rules changed."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        serializer = PinListWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        source_filter, error = _resolve_source_saved_filter(profile, data)
        if error is not None:
            return error

        # Captured before anything is applied, so the resync decision below
        # compares the real before/after rather than assuming a change.
        before = (pin_list.is_smart, pin_list.smart_filter, pin_list.smart_boundary)

        if "name" in data:
            if PinList.objects.for_profile(profile).filter(name=data["name"]).exclude(pk=pin_list.pk).exists():
                return Response({"error": "You already have a list with that name."}, status=400)
            pin_list.name = data["name"]
        if "description" in data:
            pin_list.description = data["description"]
        if "is_smart" in data:
            pin_list.is_smart = data["is_smart"]
        if "smart_boundary" in data:
            pin_list.smart_boundary = data["smart_boundary"]
        if "smart_filter" in data:
            pin_list.smart_filter = data["smart_filter"]
        if "source_saved_filter_uuid" in data:
            pin_list.source_saved_filter = source_filter
            # Pointing a list at a filter copies that filter's criteria in;
            # detaching it (null) leaves the last snapshot in place, matching
            # PinListEditView and the SET_NULL on the FK itself.
            if source_filter is not None:
                pin_list.smart_filter = source_filter.criteria

        if pin_list.smart_filter is not None:
            try:
                validate_criteria_ownership(pin_list.smart_filter, profile)
            except CriteriaOwnershipError as exc:
                return Response({"error": str(exc)}, status=400)

        pin_list.save()

        after = (pin_list.is_smart, pin_list.smart_filter, pin_list.smart_boundary)
        if before != after:
            resync_smart_list(pin_list)

        pin_list.refresh_from_db()
        return Response(PinListDetailSerializer(pin_list).data)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, list_slug: str) -> Response:
        """Delete one of the caller's lists. The pins on it are untouched."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)
        pin_list.delete()
        return Response(status=204)


class PinListItemsView(PaginatedListMixin, ExternalApiView):
    """The pins on one of the caller's lists: GET, add (POST), or remove (DELETE).

    DELETE carries a body, which is unusual but deliberate: removing a set of
    pins in one call is what an offline client replaying a queued batch needs,
    and encoding hundreds of uuids in a query string is not viable.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LISTS_READ}),
        "POST": frozenset({ApiKeyScope.LISTS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    @extend_schema(responses={200: PinListItemSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, list_slug: str) -> Response:
        """Return one page of the list's items, in display order."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        queryset = PinListItem.objects.for_list(pin_list).select_related("pin").order_by("order", "created", "pk")
        return self.paginated_response(queryset, PinListItemSerializer, request)

    @extend_schema(request=PinListItemsWriteSerializer, responses={200: PinListItemsAddResponseSerializer, 404: ErrorSerializer})
    def post(self, request: Request, list_slug: str) -> Response:
        """Add pins to the list, skipping duplicates and honoring the per-list cap.

        Uuids that name no pin of the caller's are silently dropped rather than
        refused - an offline client replaying a queued batch should not have
        the whole batch fail because one pin was deleted elsewhere meanwhile.
        """
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        serializer = PinListItemsWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pins = list(Pin.objects.filter(profile=request.user.profile, uuid__in=serializer.validated_data["pin_uuids"]))
        result = add_pins_to_list(pin_list, pins)
        return Response({"added": result.added, "skipped_over_cap": result.skipped_over_cap, "max_pins": result.max_pins})

    @extend_schema(request=PinListItemsDeleteSerializer, responses={200: PinListItemsRemoveResponseSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, list_slug: str) -> Response:
        """Remove the named pins from the list, whatever their provenance."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        serializer = PinListItemsDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pin_ids = list(Pin.objects.filter(profile=request.user.profile, uuid__in=serializer.validated_data["pin_uuids"]).values_list("pk", flat=True))
        removed = remove_pins_from_list(pin_list, pin_ids) if pin_ids else 0
        return Response({"removed": removed})


class PinListItemsReorderView(ExternalApiView):
    """POST: renumber a list's items into the submitted order."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    @extend_schema(request=PinListItemsReorderSerializer, responses={200: PinListItemsReorderResponseSerializer, 404: ErrorSerializer})
    def post(self, request: Request, list_slug: str) -> Response:
        """Set each item's order to its index in ``item_ids``.

        Ids that aren't on this list are ignored rather than rejected, matching
        the web UI's drag-and-drop behavior: a stale id from another tab should
        not fail the whole reorder.
        """
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        serializer = PinListItemsReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reordered = reorder_list_items(pin_list, serializer.validated_data["item_ids"])
        return Response({"reordered": reordered})


class PinListResyncView(ExternalApiView):
    """POST: recompute a smart list's membership from its current rules, right now.

    Runs synchronously, matching the internal behavior - ``resync_smart_list``
    is called inline by the list-edit view too, and there is no Celery task for
    it. Normally unnecessary, since membership is kept current by a Pin
    post-save signal; it exists for the case where a client has reason to
    believe the list has drifted.

    Rate-limited far more tightly than an ordinary write (see
    :class:`ExternalApiResyncThrottle`): the work this does is unbounded in the
    caller's own pin count, so it is the one endpoint here where a cheap
    request can buy expensive server-side work.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LISTS_WRITE}),
    }
    #: The standard three plus the resync-specific cap - a resync still counts
    #: against the burst and write budgets as well.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, ExternalApiResyncThrottle]

    @extend_schema(request=None, responses={200: PinListResyncResponseSerializer, 404: ErrorSerializer})
    def post(self, request: Request, list_slug: str) -> Response:
        """Fully recompute the list's membership and return the resulting pin count."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        resync_smart_list(pin_list)
        return Response({"pin_count": pin_list.items.count()})


class SavedFiltersView(PaginatedListMixin, ExternalApiView):
    """The caller's saved main-map filters: GET pages through them, POST creates one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LISTS_READ}),
        "POST": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    @extend_schema(responses={200: SavedFilterSerializer(many=True)})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's saved filters."""
        queryset = SavedFilter.objects.filter(profile=request.user.profile).order_by("order", "-created", "pk")
        return self.paginated_response(queryset, SavedFilterSerializer, request)

    @extend_schema(request=SavedFilterWriteSerializer, responses={201: SavedFilterSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Create a saved filter owned by the caller."""
        serializer = SavedFilterWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        criteria = data.get("criteria") or {}
        try:
            validate_criteria_ownership(criteria, profile)
        except CriteriaOwnershipError as exc:
            return Response({"error": str(exc)}, status=400)

        if SavedFilter.objects.name_taken_for(profile, data["name"]):
            return Response({"error": "You already have a saved filter with that name."}, status=400)

        saved_filter = SavedFilter.objects.create(
            profile=profile,
            name=data["name"],
            icon=data.get("icon") or "bookmark",
            criteria=criteria,
            order=data.get("order", 0),
        )
        return Response(SavedFilterSerializer(saved_filter).data, status=201)


class SavedFilterDetailView(ExternalApiView):
    """One of the caller's saved filters: GET it, PATCH it, or DELETE it.

    A PATCH that changes ``criteria`` also resyncs every smart list derived
    from this filter. ``PinList.smart_filter`` is a one-time *copy*, not a live
    reference, so skipping that would leave those lists silently stale - the
    response reports how many were refreshed as ``lists_resynced``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LISTS_READ}),
        "PATCH": frozenset({ApiKeyScope.LISTS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    def _get_filter(self, request: Request, filter_uuid: UUID) -> SavedFilter | None:
        """The caller's saved filter with this uuid, or None."""
        return SavedFilter.objects.filter(uuid=filter_uuid, profile=request.user.profile).first()

    @extend_schema(responses={200: SavedFilterSerializer, 404: ErrorSerializer})
    def get(self, request: Request, filter_uuid: UUID) -> Response:
        """Return one of the caller's saved filters."""
        saved_filter = self._get_filter(request, filter_uuid)
        if saved_filter is None:
            return Response({"error": "No such saved filter."}, status=404)
        return Response(SavedFilterSerializer(saved_filter).data)

    @extend_schema(request=SavedFilterWriteSerializer, responses={200: SavedFilterUpdateResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, filter_uuid: UUID) -> Response:
        """Apply a partial update, resyncing derived smart lists when criteria change."""
        saved_filter = self._get_filter(request, filter_uuid)
        if saved_filter is None:
            return Response({"error": "No such saved filter."}, status=404)

        serializer = SavedFilterWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        criteria_changed = "criteria" in data and data["criteria"] != saved_filter.criteria
        if "criteria" in data:
            try:
                validate_criteria_ownership(data["criteria"], profile)
            except CriteriaOwnershipError as exc:
                return Response({"error": str(exc)}, status=400)

        if "name" in data:
            if SavedFilter.objects.name_taken_for(profile, data["name"], exclude_pk=saved_filter.pk):
                return Response({"error": "You already have a saved filter with that name."}, status=400)
            saved_filter.name = data["name"]
        if "icon" in data:
            saved_filter.icon = data["icon"] or "bookmark"
        if "criteria" in data:
            saved_filter.criteria = data["criteria"]
        if "order" in data:
            saved_filter.order = data["order"]
        saved_filter.save()

        lists_resynced = resync_lists_for_saved_filter(saved_filter) if criteria_changed else 0

        payload = SavedFilterSerializer(saved_filter).data
        payload["lists_resynced"] = lists_resynced
        return Response(payload)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, filter_uuid: UUID) -> Response:
        """Delete one of the caller's saved filters.

        Lists derived from it keep their last-copied criteria and simply stop
        tracking further edits (the FK is SET_NULL).
        """
        saved_filter = self._get_filter(request, filter_uuid)
        if saved_filter is None:
            return Response({"error": "No such saved filter."}, status=404)
        saved_filter.delete()
        return Response(status=204)


class LabelsView(PaginatedListMixin, ExternalApiView):
    """Labels visible to the caller: GET pages through them, POST creates one.

    **The ``.with_customizations_for(profile)`` call below is load-bearing and
    must never be dropped.** ``Label._get_customization`` reads the
    ``_user_customizations`` attribute that prefetch populates, and returns
    "no customization" when the attribute is absent. Without the prefetch the
    ``effective_name``/``effective_icon``/``effective_color``/``is_customized``
    fields do not merely become an N+1 - they silently serialize the *wrong*
    values, reporting the label's own styling for every caller who has
    overridden it. There is no error; the data is just quietly incorrect.

    Filters: ``kind``, ``is_global``, ``q`` (name contains), ``parent_uuid``.
    ``?with_counts=true`` adds ``pin_count``/``location_count``, opt-in because
    each is a correlated subquery per row.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LABELS_READ}),
        "POST": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(parameters=[LabelQuerySerializer], responses={200: LabelSerializer(many=True), 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the labels visible to the caller."""
        serializer = LabelQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        profile = request.user.profile

        queryset = Label.objects.visible_to(profile).with_customizations_for(profile).ordered()
        if kind := params.get("kind"):
            queryset = queryset.filter(kind=kind)
        if (is_global := params.get("is_global")) is not None:
            queryset = queryset.filter(profile__isnull=True) if is_global else queryset.filter(profile=profile)
        if q := params.get("q"):
            queryset = queryset.filter(name__icontains=q)
        if parent_uuid := params.get("parent_uuid"):
            parent = Label.objects.visible_to(profile).filter(uuid=parent_uuid).first()
            if parent is None:
                return Response({"error": "No such parent label."}, status=400)
            queryset = queryset.filter(parents=parent)
        # with_pin_counts() supplies its own Prefetch("parents", ...); adding a
        # plain prefetch_related("parents") alongside it makes Django refuse
        # the queryset outright ("lookup was already seen with a different
        # queryset"), so the two are deliberately mutually exclusive here.
        queryset = queryset.with_pin_counts() if params.get("with_counts") else queryset.prefetch_related("parents")

        return self.paginated_response(queryset, LabelSerializer, request)

    @extend_schema(request=LabelWriteSerializer, responses={201: LabelSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Create a label owned by the caller.

        ``profile`` is always the caller's own - a client can never create a
        global label, which is a site-wide object reserved for staff.
        """
        serializer = LabelWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        if not data.get("kind"):
            return Response({"error": "kind is required."}, status=400)

        parents, error = _resolve_parent_labels(profile, data)
        if error is not None:
            return error

        label = Label.objects.create(
            profile=profile,
            name=data["name"],
            description=data.get("description") or None,
            kind=data["kind"],
            color=data.get("color") or None,
            icon=data.get("icon") or None,
            order=data.get("order", 0),
            allow_auto_tag=data.get("allow_auto_tag", True),
            keywords=data.get("keywords") or None,
        )
        if parents:
            # A brand-new label has no descendants, so no assignment can close
            # a loop - the guard is applied on update, where it can.
            label.parents.set(parents)

        return Response(LabelSerializer(_reload_label(label, profile)).data, status=201)


class LabelDetailView(ExternalApiView):
    """One label visible to the caller: GET it, PATCH it, or DELETE it.

    GET works for any visible label, including global ones. Writes do not:
    a global label is shared by every user on the site, and a protected one
    (e.g. the built-in "Visited" status) is depended on by the application
    itself, so both are refused with 403. Use the ``customization/``
    sub-resource to restyle a global label for yourself.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.LABELS_READ}),
        "PATCH": frozenset({ApiKeyScope.LABELS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(responses={200: LabelSerializer, 404: ErrorSerializer})
    def get(self, request: Request, label_uuid: UUID) -> Response:
        """Return one label visible to the caller."""
        label = _get_label(request, label_uuid)
        if label is None:
            return Response({"error": "No such label."}, status=404)
        return Response(LabelSerializer(label).data)

    @extend_schema(request=LabelWriteSerializer, responses={200: LabelSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, label_uuid: UUID) -> Response:
        """Apply a partial update to one of the caller's own labels."""
        label = _get_label(request, label_uuid)
        if label is None:
            return Response({"error": "No such label."}, status=404)
        if (refusal := _refuse_label_write(label)) is not None:
            return refusal

        serializer = LabelWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        parents, error = _resolve_parent_labels(profile, data)
        if error is not None:
            return error

        if "name" in data:
            label.name = data["name"]
        if "description" in data:
            label.description = data.get("description") or None
        if "color" in data:
            label.color = data.get("color") or None
        if "icon" in data:
            label.icon = data.get("icon") or None
        if "order" in data:
            label.order = data["order"]
        if "allow_auto_tag" in data:
            label.allow_auto_tag = data["allow_auto_tag"]
        if "keywords" in data:
            label.keywords = data.get("keywords") or None
        # `kind` is deliberately ignored on update - see LabelWriteSerializer.
        label.save()

        if "parent_uuids" in data:
            parent_ids = [parent.pk for parent in parents]
            if would_create_cycle(label, parent_ids):
                return Response({"error": "That parent would create a loop in the label hierarchy."}, status=400)
            label.parents.set(parents)

        return Response(LabelSerializer(_reload_label(label, profile)).data)

    @extend_schema(responses={204: None, 403: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, label_uuid: UUID) -> Response:
        """Delete one of the caller's own labels. Pins carrying it are untouched."""
        label = _get_label(request, label_uuid)
        if label is None:
            return Response({"error": "No such label."}, status=404)
        if (refusal := _refuse_label_write(label)) is not None:
            return refusal
        label.delete()
        return Response(status=204)


class LabelCustomizationView(ExternalApiView):
    """The caller's private display overrides for one label.

    Works for *any* label the caller can see, global ones included - this is
    the only way a client changes how a shared label appears to its user,
    since the label itself is not theirs to edit. Overrides are per-profile and
    invisible to everyone else.

    PUT replaces the override set; an empty submission (or one whose fields are
    all blank) deletes it, restoring the label's own styling. Both verbs return
    the refreshed label so a client never has to re-fetch to redraw.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.LABELS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(request=LabelCustomizationSerializer, responses={200: LabelSerializer, 404: ErrorSerializer})
    def put(self, request: Request, label_uuid: UUID) -> Response:
        """Set (or clear) the caller's overrides for this label."""
        label = _get_label(request, label_uuid)
        if label is None:
            return Response({"error": "No such label."}, status=404)

        serializer = LabelCustomizationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        upsert_label_customization(
            profile,
            label,
            name=data.get("name"),
            icon=data.get("icon"),
            color=data.get("color"),
        )
        return Response(LabelSerializer(_reload_label(label, profile)).data)

    @extend_schema(responses={200: LabelSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, label_uuid: UUID) -> Response:
        """Remove the caller's overrides, restoring the label's own styling."""
        label = _get_label(request, label_uuid)
        if label is None:
            return Response({"error": "No such label."}, status=404)

        profile = request.user.profile
        clear_label_customization(profile, label)
        return Response(LabelSerializer(_reload_label(label, profile)).data)


class LabelMergeView(ExternalApiView):
    """POST: merge other labels into the one named in the URL.

    The URL label is the **target** and survives; the ``source_uuids`` in the
    body are consumed - their pins, wikis, images or profile assignments move
    onto the target, their children are reparented onto it, and the sources
    themselves are deleted.

    **This is destructive and cannot be undone.** Merging is not covered by the
    Undo History framework: once the sources are deleted there is no staged
    entry to restore them from, and re-creating labels with the same names
    would not restore which pins carried which. Clients should confirm with the
    user before calling this.

    Sources must be the caller's own, unprotected, and of the target's kind.
    Global labels can never be a source - they are shared by every user, so
    consuming one would destroy other people's data.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(
        request=LabelMergeSerializer,
        responses={200: LabelMergeResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer},
        description="Merge labels into this one. Destructive and NOT undoable - the source labels are deleted.",
    )
    def post(self, request: Request, label_uuid: UUID) -> Response:
        """Merge the named source labels into this one."""
        target = _get_label(request, label_uuid)
        if target is None:
            return Response({"error": "No such label."}, status=404)

        serializer = LabelMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile

        sources = list(Label.objects.filter(uuid__in=serializer.validated_data["source_uuids"], profile=profile))
        if not sources:
            return Response({"error": "No labels to merge."}, status=400)

        # Captured before the merge - the source rows are gone afterwards.
        merged_uuids = [str(source.uuid) for source in sources]
        try:
            result = merge_labels(target=target, sources=sources, profile=profile)
        except LabelMergeError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(
            {
                "target": LabelSerializer(_reload_label(target, profile)).data,
                "merged_uuids": merged_uuids,
                "pins_moved": result.pins_moved,
            }
        )




class PushDeviceDetailView(ExternalApiView):
    """DELETE: unregister one of the caller's push devices by its uuid."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.PUSH_MANAGE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, device_uuid: UUID) -> Response:
        """Revoke the device; another profile's device uuid reads as not found."""
        if not unregister_device(request.user.profile, device_uuid):
            return Response({"error": "No such device."}, status=404)
        return Response(status=204)


def _friend_identity(viewer: Profile, subject: Profile) -> dict[str, Any]:
    """Shape ``subject`` for ``FriendProfileSerializer`` as ``viewer`` may see them.

    Always routed through ``services.identity_visibility.resolve_visible_identity``
    rather than read off the model, so a profile whose privacy settings don't
    permit ``viewer`` is masked here exactly as it is in the web UI. The
    ``uuid`` is still returned when masked - it is an opaque handle the caller
    needs in order to act on the relationship, and it discloses no identity.

    Args:
        viewer: The profile doing the looking.
        subject: The profile being displayed.

    Returns:
        A dict matching ``FriendProfileSerializer``'s fields.
    """
    identity = resolve_visible_identity(viewer, subject)
    masked = identity["is_masked"]
    return {
        "uuid": subject.uuid,
        "username": identity["display_name"],
        "slug": None if masked else subject.slug,
        "avatar_url": identity["display_avatar_url"],
        "is_masked": masked,
    }


def _serialize_friendship(viewer: Profile, friendship: Friendship) -> dict[str, Any]:
    """Shape one ``Friendship`` from ``viewer``'s point of view.

    ``status`` and ``relationship_type`` are passed through untouched so the
    wire values stay the model's own capitalized strings.

    Args:
        viewer: The profile the relationship is being described to.
        friendship: The relationship row.

    Returns:
        A dict matching ``FriendshipSerializer``'s fields.
    """
    outgoing = friendship.from_profile_id == viewer.pk
    other = friendship.to_profile if outgoing else friendship.from_profile
    return {
        "profile": _friend_identity(viewer, other),
        "status": friendship.status,
        "relationship_type": friendship.relationship_type,
        "direction": "outgoing" if outgoing else "incoming",
        "message": friendship.request_message,
        "created": friendship.created,
        "updated": friendship.updated,
    }


class FriendsView(ExternalApiView):
    """The caller's friend relationships: GET lists them, POST requests a new one.

    GET defaults to accepted friendships only; ``?status=Requested`` surfaces
    the pending queue. Note the capitalization - ``FriendshipStatus`` values
    are capitalized on the wire ("Accepted", "Requested"), unlike every other
    enum on this surface.

    POST evaluates the target's ``friend_request_visibility`` and both
    profiles' ``community_enabled`` *before* the relationship is touched, and
    answers a refusal with the same 404 an unknown uuid gets - a distinguishing
    403 would confirm the profile exists to someone its owner has chosen not
    to be discoverable by.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SOCIAL_READ}),
        "POST": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    @extend_schema(parameters=[FriendListQuerySerializer], responses={200: FriendListResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's relationships."""
        serializer = FriendListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        profile = request.user.profile

        try:
            page = list_friendships(
                profile,
                status=params.get("status") or FriendshipStatus.ACCEPTED,
                cursor=params.get("cursor") or None,
                limit=params.get("limit") or DEFAULT_FRIEND_PAGE_SIZE,
            )
        except FriendshipActionError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(
            {
                "results": [_serialize_friendship(profile, friendship) for friendship in page.friendships],
                "next_cursor": page.next_cursor,
            }
        )

    @extend_schema(request=FriendRequestCreateSerializer, responses={200: FriendshipSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Send a friend request, or auto-accept one already pending in reverse."""
        serializer = FriendRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        actor = request.user.profile

        target = Profile.objects.filter(uuid=data["profile_uuid"]).select_related("user").first()

        # One 404 covers all four refusals - unknown uuid, self, community off
        # on either side, and a visibility setting that excludes the caller.
        # Any of them answering differently would confirm the profile exists.
        if (
            target is None
            or target.pk == actor.pk
            or not target.community_enabled
            or not actor.community_enabled
            or not Profile.visibility_permits(target.friend_request_visibility, target, actor)
        ):
            return Response({"error": "No such profile."}, status=404)

        friendship = request_or_accept_friendship(actor, target, data.get("message") or None)
        if friendship is None:
            return Response({"error": "Could not send that friend request."}, status=400)
        return Response(_serialize_friendship(actor, friendship))


class FriendDetailView(ExternalApiView):
    """DELETE: end an existing friendship with the named profile."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, profile_uuid: UUID) -> Response:
        """Remove the friendship; an unknown or unrelated uuid reads as not found."""
        target = Profile.objects.filter(uuid=profile_uuid).first()
        if target is None:
            return Response({"error": "No such profile."}, status=404)
        try:
            remove_friend(request.user.profile, target)
        except FriendshipNotFoundError as exc:
            return Response({"error": str(exc)}, status=404)
        return Response(status=204)


class FriendActionView(ExternalApiView):
    """Base for the single-verb friendship transitions.

    Each subclass supplies only ``service_action``; the POST handler, the
    target lookup and the error-to-status mapping live here once. That mapping
    is the reason the service raises three distinct exception types:
    ``FriendshipNotFoundError`` is a 404, ``FriendLimitExceededError`` a 403
    (understood and refused), and anything else a 400.

    ``service_action`` is an overridden method rather than a plain callable
    class attribute: mypy binds a ``Callable``-typed attribute as a method and
    strips its first parameter, so the ``(actor, target)`` signature could not
    be expressed that way without a cast.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Apply this view's ``services.friendship`` transition.

        Args:
            actor: The calling profile.
            target: The other profile in the relationship.

        Returns:
            The updated relationship.

        Raises:
            NotImplementedError: The subclass did not supply a transition.
        """
        raise NotImplementedError

    def _resolve_target(self, profile_uuid: UUID) -> Profile | None:
        """Look up the other profile in the relationship.

        Args:
            profile_uuid: The target's public uuid.

        Returns:
            The profile, or None when no such uuid exists.
        """
        return Profile.objects.filter(uuid=profile_uuid).select_related("user").first()

    @extend_schema(request=None, responses={200: FriendshipSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, profile_uuid: UUID) -> Response:
        """Apply this view's transition to the caller's relationship with the target."""
        target = self._resolve_target(profile_uuid)
        if target is None:
            return Response({"error": "No such profile."}, status=404)

        actor = request.user.profile
        try:
            friendship = self.service_action(actor, target)
        except FriendshipNotFoundError as exc:
            return Response({"error": str(exc)}, status=404)
        except FriendLimitExceededError as exc:
            return Response({"error": str(exc)}, status=403)
        except FriendshipActionError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(_serialize_friendship(actor, friendship))


class FriendAcceptView(FriendActionView):
    """POST: accept the target's pending friend request."""

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Accept the target's pending request."""
        return accept_friend_request(actor, target)


class FriendRejectView(FriendActionView):
    """POST: decline the target's friend request; they may re-send later."""

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Decline the target's request."""
        return reject_friend_request(actor, target)


class FriendIgnoreView(FriendActionView):
    """POST: ignore the target's friend request - silently and permanently.

    Distinct from reject: no notification is sent and the requester can never
    re-send (``FriendshipStatus.can_request`` excludes ``Ignored``).
    """

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Ignore the target's request."""
        return ignore_friend_request(actor, target)


class FriendBlockView(FriendActionView):
    """POST: block the target, creating the relationship row if there isn't one.

    The only transition here that works against a stranger - that is what
    blocking is for.
    """

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Block the target."""
        return block_profile(actor, target)


class FriendMuteView(FriendActionView):
    """POST: mute an existing relationship with the target."""

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Mute the relationship with the target."""
        return mute_profile(actor, target)


class FriendInvitesView(ExternalApiView):
    """POST: invite someone to connect by email address.

    Answers ``200 {"result": "sent"}`` in every case that is not a validation
    error or a rate limit - registered, unregistered, privacy-rejected, and
    mail-send-failed alike. That invariance is the entire security property:
    any branch in the status code, body or headers would let a caller
    enumerate site membership one address at a time.

    ``subscription_role`` is deliberately not accepted (see
    ``FriendInviteSerializer``).
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    @extend_schema(request=FriendInviteSerializer, responses={200: FriendInviteResponseSerializer, 400: ErrorSerializer, 429: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Send the invitation, returning the same response whatever happened."""
        serializer = FriendInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            invite_by_email(
                request.user.profile,
                data["email"],
                data.get("message") or "",
                signup_url_builder=lambda token: request.build_absolute_uri(f"/signup/?invite={token}"),
            )
        except InviteValidationError as exc:
            return Response({"error": str(exc)}, status=400)
        except InviteRateLimitedError as exc:
            return Response({"error": str(exc)}, status=429)

        return Response({"result": "sent"})


def _resolve_profile(profile_slug: str) -> Profile | None:
    """Look up a profile by slug, falling back to uuid.

    Profiles are slug-addressed on the web, but a sync client that cached a
    uuid should not break when the owner renames themselves.

    Args:
        profile_slug: A profile slug or a uuid string.

    Returns:
        The profile, or None when neither form matches.
    """
    profile = Profile.objects.filter(slug=profile_slug).select_related("user").first()
    if profile is not None:
        return profile
    try:
        return Profile.objects.filter(uuid=UUID(profile_slug)).select_related("user").first()
    except (ValueError, AttributeError, TypeError):
        return None


class ProfileDetailView(ExternalApiView):
    """GET a profile the caller may see; PATCH the caller's own.

    A profile whose ``profile_visibility`` excludes the caller answers 404,
    not 403 - matching ``controllers.userprofile.ViewProfileView``, which
    raises ``Http404`` for the same reason: a 403 would confirm the account
    exists to exactly the people its owner excluded.

    PATCH only ever touches the caller's own profile. Any other slug is a 404
    for the same reason, rather than a 403.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        # Reading someone else's profile is a social read, not merely
        # "profile:read" (which covers only the caller's own uuid).
        "GET": frozenset({ApiKeyScope.PROFILE_READ, ApiKeyScope.SOCIAL_READ}),
        "PATCH": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    @extend_schema(responses={200: ProfileDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, profile_slug: str) -> Response:
        """Return the profile as this caller is permitted to see it."""
        viewer = request.user.profile
        target = _resolve_profile(profile_slug)
        if target is None or not target.can_view_profile(viewer):
            return Response({"error": "No such profile."}, status=404)

        is_self = target.pk == viewer.pk
        friendship = None if is_self else Friendship.objects.all().between(viewer, target)

        payload: dict[str, Any] = {
            "uuid": target.uuid,
            "username": target.username,
            "slug": target.slug,
            "avatar_url": target.avatar.url if target.avatar else None,
            "bio": target.bio,
            "area": target.area,
            "started_exploring": target.started_exploring,
            "is_self": is_self,
            "friendship_status": friendship.status if friendship else None,
            "contact": None,
            "visibility": None,
        }

        if target.can_view_contact_info(viewer):
            payload["contact"] = {
                "phone_number": target.phone_number,
                "signal_username": target.signal_username,
                "discord_username": target.discord_username,
                "whatsapp_number": target.whatsapp_number,
                "telegram_username": target.telegram_username,
                "matrix_handle": target.matrix_handle,
            }

        # Your privacy configuration is itself private - never served for
        # anyone else's profile, however visible that profile is to you.
        if is_self:
            payload["visibility"] = {name: getattr(target, name) for name in _COMMUNITY_GATED_VISIBILITY_FIELDS}

        return Response(ProfileDetailSerializer(payload).data)

    @extend_schema(request=ProfileUpdateSerializer, responses={200: ProfileDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, profile_slug: str) -> Response:
        """Apply a partial update to the caller's own profile."""
        viewer = request.user.profile
        target = _resolve_profile(profile_slug)
        # Not 403: a caller who may not edit this profile must not learn it exists.
        if target is None or target.pk != viewer.pk:
            return Response({"error": "No such profile."}, status=404)

        serializer = ProfileUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data:
            return self.get(request, profile_slug)

        for field, value in data.items():
            setattr(target, field, value)
        # Deliberately a full save() rather than update_fields: Profile.save()
        # coerces the community-gated visibility and wiki-sync fields when
        # community_enabled is off, and those coercions must be persisted even
        # though the caller never named those fields.
        target.save()

        return self.get(request, profile_slug)


class ProfileNotesView(ExternalApiView):
    """The caller's own private notes about another profile.

    Notes are always the *caller's*, never the subject's - the subject can
    never see them, and a subject reading their own page gets their notes
    about themselves, not other people's notes about them. Several notes per
    subject are allowed, matching the model.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SOCIAL_READ}),
        "POST": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    @extend_schema(responses={200: ProfileNoteSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, profile_slug: str) -> Response:
        """List the caller's notes about the named profile."""
        viewer = request.user.profile
        subject = _resolve_profile(profile_slug)
        if subject is None or not subject.can_view_profile(viewer):
            return Response({"error": "No such profile."}, status=404)
        notes = ProfileNote.objects.for_pair(viewer, subject)
        return Response(ProfileNoteSerializer(notes, many=True).data)

    @extend_schema(request=ProfileNoteWriteSerializer, responses={201: ProfileNoteSerializer, 404: ErrorSerializer})
    def post(self, request: Request, profile_slug: str) -> Response:
        """Add one note about the named profile."""
        viewer = request.user.profile
        subject = _resolve_profile(profile_slug)
        if subject is None or not subject.can_view_profile(viewer):
            return Response({"error": "No such profile."}, status=404)

        serializer = ProfileNoteWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = ProfileNote.objects.create(author=viewer, subject=subject, content=serializer.validated_data["content"])
        return Response(ProfileNoteSerializer(note).data, status=201)


class ProfileNoteDetailView(ExternalApiView):
    """PATCH or DELETE one of the caller's own notes about a profile."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PATCH": frozenset({ApiKeyScope.SOCIAL_WRITE}),
        "DELETE": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    def _get_note(self, viewer: Profile, profile_slug: str, note_uuid: UUID) -> ProfileNote | None:
        """The caller's own note with this uuid about this subject, or None.

        Scoped by author first, so another user's note uuid is
        indistinguishable from one that does not exist.

        Args:
            viewer: The calling profile, which must be the note's author.
            profile_slug: The subject the note is about.
            note_uuid: The note's public uuid.

        Returns:
            The note, or None when nothing matches.
        """
        subject = _resolve_profile(profile_slug)
        if subject is None:
            return None
        return ProfileNote.objects.for_pair(viewer, subject).filter(uuid=note_uuid).first()

    @extend_schema(request=ProfileNoteWriteSerializer, responses={200: ProfileNoteSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, profile_slug: str, note_uuid: UUID) -> Response:
        """Replace the note's content."""
        note = self._get_note(request.user.profile, profile_slug, note_uuid)
        if note is None:
            return Response({"error": "No such note."}, status=404)

        serializer = ProfileNoteWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note.content = serializer.validated_data["content"]
        note.save(update_fields=["content", "updated"])
        return Response(ProfileNoteSerializer(note).data)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, profile_slug: str, note_uuid: UUID) -> Response:
        """Delete the note."""
        note = self._get_note(request.user.profile, profile_slug, note_uuid)
        if note is None:
            return Response({"error": "No such note."}, status=404)
        note.delete()
        return Response(status=204)


class NotificationsView(ExternalApiView):
    """GET: one page of the caller's notification inbox, newest first."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.NOTIFICATIONS_READ}),
    }

    @extend_schema(parameters=[NotificationListQuerySerializer], responses={200: NotificationListResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's notifications."""
        serializer = NotificationListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        profile = request.user.profile

        try:
            page = list_notifications(
                profile,
                unread_only=params.get("unread_only", False),
                cursor=params.get("cursor") or None,
                limit=params.get("limit") or DEFAULT_NOTIFICATION_PAGE_SIZE,
            )
        except InvalidNotificationCursorError as exc:
            return Response({"error": str(exc)}, status=400)

        results = [
            {
                "uuid": row.uuid,
                "notification_type": row.notification_type,
                "status": row.status,
                "importance": row.importance,
                "title": row.title,
                "message": row.message,
                "url": row.url,
                "created": row.created,
                "source_profile": _friend_identity(profile, row.source_profile) if row.source_profile else None,
            }
            for row in page.notifications
        ]
        return Response(
            {
                "results": results,
                "next_cursor": page.next_cursor,
                "unread_count": unread_count(profile),
            }
        )


class NotificationDetailView(ExternalApiView):
    """POST: mark one of the caller's notifications read."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.NOTIFICATIONS_WRITE}),
    }

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request, notification_uuid: UUID) -> Response:
        """Mark it read, answering 204 whether or not a row actually matched.

        A 404 for "no such notification" would double as an oracle: a caller
        could learn whether a given uuid belongs to somebody by whether the
        acknowledgement succeeded. Since the operation is idempotent and
        nothing is returned either way, one status covers both.
        """
        mark_notification_read(request.user.profile, notification_uuid)
        return Response(status=204)


class NotificationsReadAllView(ExternalApiView):
    """POST: mark every unread notification of the caller's read."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.NOTIFICATIONS_WRITE}),
    }

    @extend_schema(request=None, responses={200: UnreadCountSerializer})
    def post(self, request: Request) -> Response:
        """Clear the caller's unread notifications and echo the resulting count."""
        mark_all_read(request.user.profile)
        return Response({"unread_count": 0})


class NotificationsUnreadCountView(ExternalApiView):
    """GET: how many unread notifications the caller has.

    Split out from the list endpoint so a client polling only for a badge
    count doesn't page through notification bodies to get it.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.NOTIFICATIONS_READ}),
    }

    @extend_schema(responses={200: UnreadCountSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's unread notification count."""
        return Response({"unread_count": unread_count(request.user.profile)})


class NotificationDeliveryPreferencesView(ExternalApiView):
    """GET the caller's per-type notification delivery preferences; PATCH to change them.

    Distinct from ``AccountSettingsView`` (``/settings/``), which covers
    general account preferences - this is the notification matrix specifically.

    Exposes exactly the stems ``NotificationPreference`` defines, which is a
    strict subset of ``NotificationType``: most notification types have no
    per-type delivery control at all, and none is invented here.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.NOTIFICATIONS_READ}),
        "PATCH": frozenset({ApiKeyScope.NOTIFICATIONS_WRITE}),
    }

    @extend_schema(responses={200: NotificationPreferenceSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's full preference document."""
        return Response(serialize_preferences(get_preferences(request.user.profile)))

    @extend_schema(request=NotificationPreferenceSerializer, responses={200: NotificationPreferenceSerializer, 400: ErrorSerializer})
    def patch(self, request: Request) -> Response:
        """Apply a partial preference update and return the resulting document."""
        serializer = NotificationPreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Re-read from the saved row rather than echoing the submission: the
        # service forces WhatsApp/SMS off when there is no number to deliver
        # to, and the client must see the values it actually ended up with.
        prefs = update_preferences(request.user.profile, serializer.validated_data)
        return Response(serialize_preferences(prefs))
