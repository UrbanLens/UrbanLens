"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, ClassVar

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count
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
    SafetyCheckinCreateSerializer,
    SafetyCheckinDetailSerializer,
    SafetyCheckinListResponseSerializer,
    SafetyCheckinSummarySerializer,
    SafetyCheckinUpdateSerializer,
    SafetyContactDefaultsResponseSerializer,
    SafetyContactDefaultsSerializer,
    SafetyMapAttachSerializer,
    SafetyMapSerializer,
    SafetyPartnerInviteSerializer,
    SafetyPhotoAttachSerializer,
    SafetyPhotoListResponseSerializer,
    SafetyPhotoSerializer,
    SafetyPreferenceSerializer,
    SettingsPatchSerializer,
    SettingsSerializer,
    TombstoneSyncQuerySerializer,
    TombstoneSyncResponseSerializer,
    WhoAmISerializer,
)
from urbanlens.dashboard.external_api.throttling import TIER_READ, ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinPartner, SafetyCheckinStatus, SafetyPreference
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, PinReparentError, delete_pin, move_pin_to_coordinates, reparent_pin
from urbanlens.dashboard.services.pin_suggestions import LocationHit, attach_suggestion_photos, ingest_location_hits
from urbanlens.dashboard.services.pin_sync import InvalidSyncCursorError, StaleDeletedSinceError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.profile_settings import SettingsValidationError, apply_settings_patch, read_settings
from urbanlens.dashboard.services.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.safety import (
    CheckinArchivedError,
    apply_checkin_edit,
    attach_draft_markup_map,
    cancel_checkin,
    check_in,
    create_checkin,
    default_contacts_as_input,
    delete_checkin,
    get_or_create_preference,
    invite_checkin_partner,
    remove_checkin_partner,
    resolve_contact_inputs,
    save_contact_defaults,
    validate_notifiable_contacts,
)
from urbanlens.dashboard.services.visits import visit_logging_allowed

if TYPE_CHECKING:
    from uuid import UUID

    from rest_framework.request import Request

    from urbanlens.dashboard.models.profile.model import Profile

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


class SafetyCheckinScopedView(ExternalApiView):
    """Base for every endpoint addressing one of the caller's own check-ins.

    The lookup is **owner-scoped only**. Check-ins shared with the caller as an
    accepted partner, or with them as a registered emergency contact, are
    deliberately unreachable through this surface: every write here (edit, mark
    safe, cancel, invite, delete) is an owner action, and the read shape is the
    owner's full document including the whole contact list. Serving a
    partner/contact their narrower view means modelling check-ins owned by other
    profiles - a structural change, not a filter tweak - so it is out of scope
    for this pass rather than approximated.
    """

    def _get_checkin(self, request: Request, checkin_slug: str) -> SafetyCheckin | None:
        """The key owner's check-in matching *checkin_slug* (by slug, then uuid), or None.

        Mirrors ``controllers.safety._get_checkin_by_slug``: the identifier is
        usually a real slug, but older/direct-linked check-ins are addressed by
        raw uuid. A non-uuid string makes the uuid comparison raise
        ``ValidationError`` rather than simply not matching, so that is caught
        and reported as "not found" like any other miss.

        Args:
            request: The authenticated request, whose user scopes the lookup.
            checkin_slug: The slug or uuid captured from the URL.

        Returns:
            The matching check-in, or None - callers answer None with a 404.
        """
        owned = SafetyCheckin.objects.filter(profile__user=request.user).select_related("trip", "markup_map", "profile")
        checkin = owned.filter(slug=checkin_slug).first()
        if checkin is not None:
            return checkin
        try:
            return owned.filter(uuid=checkin_slug).first()
        except (DjangoValidationError, ValueError):
            return None

    def _not_found(self) -> Response:
        """The single 404 body every check-in lookup miss answers with.

        Another profile's check-in is reported as *not found*, never forbidden -
        a 403 would confirm that a given slug names a real check-in belonging to
        someone, which is itself a disclosure on a safety feature.
        """
        return Response({"error": "No such check-in."}, status=404)

    def _detail_response(self, checkin: SafetyCheckin, *, extra: dict | None = None, status: int = 200) -> Response:
        """Serialize *checkin* as the standard detail document.

        Args:
            checkin: The check-in to serialize. Re-fetched with the annotations
                and prefetches the detail serializer expects.
            extra: Additional top-level keys to merge into the payload.
            status: HTTP status for the response.

        Returns:
            The detail response.
        """
        hydrated = (
            SafetyCheckin.objects.filter(pk=checkin.pk)
            .select_related("trip", "markup_map", "profile", "archive")
            .prefetch_related("contacts__contact_profile", "partners__profile", "partners__invited_by", "markup_maps")
            .annotate(contact_count=Count("contacts", distinct=True), partner_count=Count("partners", distinct=True))
            .first()
        )
        payload = dict(SafetyCheckinDetailSerializer(hydrated).data)
        if extra:
            payload.update(extra)
        return Response(payload, status=status)


class SafetyCheckinsView(SafetyCheckinScopedView, PaginatedListMixin):
    """GET: browse the caller's safety check-ins. POST: start a new one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyCheckinListResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's check-ins, newest deadline first."""
        queryset = (
            SafetyCheckin.objects.filter(profile__user=request.user)
            .select_related("trip", "archive")
            .annotate(contact_count=Count("contacts", distinct=True), partner_count=Count("partners", distinct=True))
            # Explicit and total: checkin_by alone ties whenever two check-ins
            # share a deadline, and an unstable sort silently drops or repeats
            # rows across page boundaries.
            .order_by("-checkin_by", "-pk")
        )
        # "Active" means "has not reached a terminal status", the same definition
        # SafetyCheckin.objects.active() uses to enforce one-active-check-in-per-scope.
        # Filtering on resolved_at instead would create a second, subtly different
        # notion of active that could disagree with the rest of the system.
        status_filter = (request.query_params.get("status") or "all").strip().lower()
        if status_filter == "active":
            queryset = queryset.exclude(status__in=SafetyCheckinStatus.resolved_statuses())
        elif status_filter == "resolved":
            queryset = queryset.filter(status__in=SafetyCheckinStatus.resolved_statuses())

        trip_slug = (request.query_params.get("trip") or "").strip()
        if trip_slug:
            queryset = queryset.filter(trip__slug=trip_slug)

        return self.paginated_response(queryset, SafetyCheckinSummarySerializer, request)

    @extend_schema(request=SafetyCheckinCreateSerializer, responses={201: SafetyCheckinDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Start a new check-in for the caller."""
        serializer = SafetyCheckinCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        trip = None
        if data.get("trip"):
            # Same membership rule as the web create flow: a trip the caller has
            # not joined is reported as not found, not as forbidden.
            trip = Trip.objects.filter(slug=data["trip"]).first()
            if trip is None or not (trip.creator_id == profile.pk or TripMembership.objects.for_trip_and_profile(trip, profile).filter(status=TripMembership.STATUS_JOINED).exists()):
                return Response({"error": "No such trip."}, status=404)

        submitted_contacts = data.get("contacts")
        if submitted_contacts is None:
            contact_inputs = default_contacts_as_input(profile)
        else:
            contact_inputs, rejections = resolve_contact_inputs(profile, submitted_contacts)
            if rejections:
                return Response({"error": " ".join(rejections)}, status=400)

        allowed, rejected = validate_notifiable_contacts(profile, contact_inputs)
        if rejected:
            # Creation is all-or-nothing, unlike an edit: the caller is choosing
            # who gets told if they go missing, and silently starting a check-in
            # with fewer contacts than asked for is the wrong failure mode.
            return Response({"error": " ".join(rejected)}, status=400)

        checkin_by = data["checkin_by"]
        title = (data.get("title") or "").strip() or f"Check-in - {checkin_by:%b} {checkin_by.day}, {checkin_by.year}"
        grace_seconds = data.get("grace_period_seconds")
        grace_period = timedelta(seconds=grace_seconds) if grace_seconds is not None else get_or_create_preference(profile).default_grace_period

        try:
            checkin = create_checkin(
                profile=profile,
                title=title,
                checkin_by=checkin_by,
                grace_period=grace_period,
                plan_details=data.get("plan_details", ""),
                contact_message=data.get("contact_message", ""),
                trip=trip,
                destination_latitude=data.get("destination_latitude"),
                destination_longitude=data.get("destination_longitude"),
                contacts=allowed,
                notify_community_wiki=data.get("notify_community_wiki", False),
            )
        except ValueError as exc:
            # An already-active check-in for this scope is a state conflict, not a
            # malformed request - a mobile client must be able to tell the two
            # apart to offer "open the existing one" instead of "fix your input".
            return Response({"error": str(exc)}, status=409)

        if data.get("markup_map"):
            attach_draft_markup_map(checkin, profile, str(data["markup_map"]))

        return self._detail_response(checkin, status=201)


class SafetyCheckinDetailApiView(SafetyCheckinScopedView):
    """GET, PATCH, or DELETE one of the caller's check-ins."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "PATCH": frozenset({ApiKeyScope.SAFETY_WRITE}),
        "DELETE": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyCheckinDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, checkin_slug: str) -> Response:
        """Return the caller's full detail document for one check-in."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        return self._detail_response(checkin)

    @extend_schema(request=SafetyCheckinUpdateSerializer, responses={200: SafetyCheckinDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def patch(self, request: Request, checkin_slug: str) -> Response:
        """Apply a partial update, honoring the same field locks as the web autosave."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()

        serializer = SafetyCheckinUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Built strictly from key *presence*: apply_checkin_edit reads None as
        # "not submitted", so passing a field the caller omitted would either
        # clobber it or invent a lock warning. See the serializer's no-default rule.
        kwargs: dict = {}
        for field in ("title", "plan_details", "contact_message", "notify_community_wiki"):
            if field in data:
                kwargs[field] = data[field]
        if "destination_latitude" in data or "destination_longitude" in data:
            kwargs["destination"] = (data.get("destination_latitude"), data.get("destination_longitude"))

        try:
            outcome = apply_checkin_edit(checkin, editor=request.user.profile, **kwargs)
        except CheckinArchivedError as exc:
            return Response({"error": str(exc)}, status=409)

        # Warnings are not errors: a locked field is silently ignored, exactly as
        # the web autosave does. The client surfaces these as toasts and keeps the
        # 200 - failing the whole request would make an unrelated field's edit
        # collateral damage.
        return self._detail_response(checkin, extra={"warnings": outcome.warnings})

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, checkin_slug: str) -> Response:
        """Delete one of the caller's check-ins, staging an Undo History entry."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        delete_checkin(checkin, request.user.profile)
        return Response(status=204)


class SafetyCheckinMarkSafeView(SafetyCheckinScopedView):
    """POST: the caller checks in, resolving the check-in."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(request=None, responses={200: SafetyCheckinDetailSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, checkin_slug: str) -> Response:
        """Mark the caller safe on this check-in."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        if checkin.is_resolved:
            return Response({"error": "This check-in has already been resolved."}, status=409)
        check_in(checkin, request.user.profile)
        return self._detail_response(checkin)


class SafetyCheckinCancelApiView(SafetyCheckinScopedView):
    """POST: cancel a check-in so it never escalates."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(request=None, responses={200: SafetyCheckinDetailSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, checkin_slug: str) -> Response:
        """Cancel this check-in."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        if checkin.is_resolved:
            return Response({"error": "This check-in has already been resolved."}, status=409)
        cancel_checkin(checkin)
        return self._detail_response(checkin)


class SafetyCheckinPartnersApiView(SafetyCheckinScopedView):
    """POST: invite a partner to one of the caller's check-ins by username.

    Partner *accept*/*decline*, and a partner's own read view of someone else's
    check-in, are out of scope this pass - see :class:`SafetyCheckinScopedView`.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(request=SafetyPartnerInviteSerializer, responses={200: SafetyCheckinDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, checkin_slug: str) -> Response:
        """Invite the named account as a partner."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()

        serializer = SafetyPartnerInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            invite_checkin_partner(checkin, inviter=request.user.profile, username=serializer.validated_data["username"].strip())
        except ValueError as exc:
            # The service's messages are already user-facing and specific
            # (unknown username, self-invite, blocked, already invited, cap
            # reached); reused verbatim so the app and the web UI say the same thing.
            return Response({"error": str(exc)}, status=400)
        return self._detail_response(checkin)


class SafetyCheckinPartnerDetailApiView(SafetyCheckinScopedView):
    """DELETE: remove a partner from one of the caller's check-ins."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyCheckinDetailSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, checkin_slug: str, partner_id: int) -> Response:
        """Remove the partner, also revoking any live connection they hold."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        partner = SafetyCheckinPartner.objects.filter(pk=partner_id, checkin=checkin).first()
        if partner is None:
            return Response({"error": "No such partner."}, status=404)
        # Also force-closes an accepted partner's open WebSocket, whose permission
        # was only checked at connect time - see services.safety.remove_checkin_partner.
        remove_checkin_partner(partner)
        return self._detail_response(checkin)


class SafetyCheckinPhotosView(SafetyCheckinScopedView, PaginatedListMixin):
    """GET: the check-in's photos. POST: attach an already-uploaded photo."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyPhotoListResponseSerializer, 404: ErrorSerializer})
    def get(self, request: Request, checkin_slug: str) -> Response:
        """Return one page of the check-in's photos, newest first."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        images = Image.objects.filter(safety_checkin=checkin).order_by("-created", "-pk")
        return self.paginated_response(images, SafetyPhotoSerializer, request)

    @extend_schema(request=SafetyPhotoAttachSerializer, responses={201: SafetyPhotoSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, checkin_slug: str) -> Response:
        """Attach one of the caller's already-uploaded images to this check-in.

        Deliberately *not* a second multipart upload path - see
        :class:`~urbanlens.dashboard.external_api.serializers.SafetyPhotoAttachSerializer`
        for why this waits on the shared upload service.
        """
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()

        serializer = SafetyPhotoAttachSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = Image.objects.filter(uuid=serializer.validated_data["image_uuid"], profile=request.user.profile).first()
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        if image.safety_checkin_id is not None and image.safety_checkin_id != checkin.pk:
            return Response({"error": "That photo is already attached to another check-in."}, status=400)
        image.safety_checkin = checkin
        image.save(update_fields=["safety_checkin", "updated"])
        return Response(SafetyPhotoSerializer(image).data, status=201)


class SafetyCheckinPhotoDetailView(SafetyCheckinScopedView):
    """DELETE: remove one photo from one of the caller's check-ins."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, checkin_slug: str, image_id: int) -> Response:
        """Delete the photo and its stored file."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        image = Image.objects.filter(pk=image_id, safety_checkin=checkin, profile=request.user.profile).first()
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        image.image.delete(save=False)
        image.delete()
        return Response(status=204)


class SafetyCheckinMapsView(SafetyCheckinScopedView):
    """GET: the check-in's maps. POST: attach one of the caller's own maps."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyMapSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, checkin_slug: str) -> Response:
        """Return the check-in's primary route map plus every attached reference map."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        maps: list[dict] = []
        if checkin.markup_map is not None:
            maps.append({"uuid": str(checkin.markup_map.uuid), "title": checkin.markup_map.title, "is_primary": True})
        maps += [{"uuid": str(m.uuid), "title": m.title, "is_primary": False} for m in checkin.markup_maps.all()]
        return Response(maps)

    @extend_schema(request=SafetyMapAttachSerializer, responses={200: SafetyMapSerializer(many=True), 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, checkin_slug: str) -> Response:
        """Attach one of the caller's existing maps as a reference map."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()

        serializer = SafetyMapAttachSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        markup_map = MarkupMap.objects.filter(uuid=serializer.validated_data["map_uuid"], profile=request.user.profile).first()
        if markup_map is None:
            return Response({"error": "No such map."}, status=400)
        if markup_map.pk == checkin.markup_map_id:
            # Same rule the web attach view enforces: services.safety._build_archive_payload
            # keys its "maps" list by the primary map first, so allowing the primary
            # map to also be attached would archive it twice.
            return Response({"error": "This map is already the check-in's primary route map."}, status=400)
        checkin.markup_maps.add(markup_map)
        return self.get(request, checkin_slug)


class SafetyCheckinMapDetailView(SafetyCheckinScopedView):
    """DELETE: detach a reference map from a check-in, without deleting the map."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, checkin_slug: str, map_uuid: UUID) -> Response:
        """Detach the map. The MarkupMap itself is left intact and reusable."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        attached = checkin.markup_maps.filter(uuid=map_uuid)
        if not attached.exists():
            return Response({"error": "No such map on this check-in."}, status=404)
        checkin.markup_maps.remove(*attached)
        return Response(status=204)


class SafetyContactDefaultsView(ExternalApiView):
    """GET or replace the caller's default emergency contacts."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "PUT": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    def _payload(self, profile: Profile, rejected: list[str] | None = None) -> dict:
        """Shape the saved defaults into the response document."""
        contacts = [
            {
                "display_name": contact_profile.username if contact_profile is not None else (label or email or ""),
                "email": email,
                "username": contact_profile.username if contact_profile is not None else None,
            }
            for contact_profile, email, label in default_contacts_as_input(profile)
        ]
        return {"contacts": contacts, "rejected": rejected or []}

    @extend_schema(responses={200: SafetyContactDefaultsResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's saved default emergency contacts."""
        return Response(self._payload(request.user.profile))

    @extend_schema(request=SafetyContactDefaultsSerializer, responses={200: SafetyContactDefaultsResponseSerializer, 400: ErrorSerializer})
    def put(self, request: Request) -> Response:
        """Replace the caller's default contacts wholesale.

        PUT rather than PATCH because the underlying
        ``services.safety.save_contact_defaults`` deletes and recreates the whole
        set - there is no per-entry addressing to PATCH against.
        """
        serializer = SafetyContactDefaultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile

        contact_inputs, rejections = resolve_contact_inputs(profile, serializer.validated_data["contacts"])
        allowed, rejected = validate_notifiable_contacts(profile, contact_inputs)
        save_contact_defaults(profile, allowed)
        return Response(self._payload(profile, rejected=[*rejections, *rejected]))


class SafetyPreferencesView(ExternalApiView):
    """GET or PATCH the caller's safety defaults (message, grace period, auto-delete)."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "PATCH": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    def _payload(self, preference: SafetyPreference) -> dict:
        """Shape a SafetyPreference into the response document."""
        return {
            "default_message": preference.default_message,
            "default_grace_period_seconds": int(preference.default_grace_period.total_seconds()),
            "auto_delete_after_days": preference.auto_delete_after_days,
        }

    @extend_schema(responses={200: SafetyPreferenceSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's safety defaults."""
        return Response(self._payload(get_or_create_preference(request.user.profile)))

    @extend_schema(request=SafetyPreferenceSerializer, responses={200: SafetyPreferenceSerializer, 400: ErrorSerializer})
    def patch(self, request: Request) -> Response:
        """Apply a partial update to the caller's safety defaults."""
        serializer = SafetyPreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        preference = get_or_create_preference(request.user.profile)

        # Presence-driven, same rule as every other PATCH here.
        update_fields: list[str] = []
        if "default_message" in data:
            preference.default_message = data["default_message"].strip()
            update_fields.append("default_message")
        if "default_grace_period_seconds" in data:
            preference.default_grace_period = timedelta(seconds=data["default_grace_period_seconds"])
            update_fields.append("default_grace_period")
        if "auto_delete_after_days" in data:
            preference.auto_delete_after_days = data["auto_delete_after_days"]
            update_fields.append("auto_delete_after_days")
        if update_fields:
            preference.save(update_fields=[*update_fields, "updated"])
        return Response(self._payload(preference))


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
