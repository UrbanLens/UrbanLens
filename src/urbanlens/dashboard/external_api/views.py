"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope
from urbanlens.dashboard.external_api.serializers import (
    AuthSessionSerializer,
    ErrorSerializer,
    LabelCustomizationSerializer,
    LabelMergeResponseSerializer,
    LabelMergeSerializer,
    LabelQuerySerializer,
    LabelSerializer,
    LabelWriteSerializer,
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
    PushDeviceRegisterSerializer,
    PushDeviceResponseSerializer,
    SavedFilterSerializer,
    SavedFilterUpdateResponseSerializer,
    SavedFilterWriteSerializer,
    SettingsPatchSerializer,
    SettingsSerializer,
    TombstoneSyncQuerySerializer,
    TombstoneSyncResponseSerializer,
    WhoAmISerializer,
)
from urbanlens.dashboard.external_api.throttling import (
    TIER_READ,
    ExternalApiBurstThrottle,
    ExternalApiReadThrottle,
    ExternalApiResyncThrottle,
    ExternalApiWriteThrottle,
)
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.filter_criteria import CriteriaOwnershipError, validate_criteria_ownership
from urbanlens.dashboard.services.labels.customization import clear_label_customization, upsert_label_customization
from urbanlens.dashboard.services.labels.hierarchy import would_create_cycle
from urbanlens.dashboard.services.labels.merge import LabelMergeError, merge_labels
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
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
from urbanlens.dashboard.services.visits import visit_logging_allowed

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.profile.model import Profile

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
