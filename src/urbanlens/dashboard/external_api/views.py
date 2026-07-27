"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

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
    JournalEntrySerializer,
    JournalQuerySerializer,
    JournalResponseSerializer,
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
    PinSuggestionCreateResponseSerializer,
    PinSuggestionCreateSerializer,
    PinSyncQuerySerializer,
    PinSyncResponseSerializer,
    PinUpdateSerializer,
    PushDeviceRegisterSerializer,
    PushDeviceResponseSerializer,
    SettingsPatchSerializer,
    SettingsSerializer,
    TombstoneSyncQuerySerializer,
    TombstoneSyncResponseSerializer,
    VisitSuggestionListResponseSerializer,
    WhoAmISerializer,
    build_photo_payload,
)
from urbanlens.dashboard.external_api.throttling import TIER_READ, ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.media_labels import MediaLabelError, set_media_labels
from urbanlens.dashboard.services.media_relevance import toggle_media_vote
from urbanlens.dashboard.services.memories.journal import get_journal_entries
from urbanlens.dashboard.services.memories.photos import create_pin_and_log_visit, log_visit_on_pin
from urbanlens.dashboard.services.photo_upload import PhotoUploadError, upload_photo
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, PinReparentError, delete_pin, move_pin_to_coordinates, reparent_pin
from urbanlens.dashboard.services.pin_suggestions import LocationHit, attach_suggestion_photos, ingest_location_hits
from urbanlens.dashboard.services.pin_sync import InvalidSyncCursorError, StaleDeletedSinceError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.profile_settings import SettingsValidationError, apply_settings_patch, read_settings
from urbanlens.dashboard.services.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.visits import accept_visit_suggestion, reject_visit_suggestion, visit_logging_allowed

if TYPE_CHECKING:
    from uuid import UUID

    from rest_framework.request import Request

logger = logging.getLogger(__name__)

#: Fixed source_key for the single hit a pin-suggestion POST produces - this
#: endpoint is one discovered place per call (mirrors PinsView.post), so there's
#: never more than one id to look up in IngestSummary.suggestion_ids_by_key.
_SUGGESTION_SOURCE_KEY = "external_api_submission"


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
