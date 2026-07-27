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
    SettingsPatchSerializer,
    SettingsSerializer,
    TombstoneSyncQuerySerializer,
    TombstoneSyncResponseSerializer,
    TripActivityCreateSerializer,
    TripActivityListQuerySerializer,
    TripActivityPositionSerializer,
    TripActivityRsvpSerializer,
    TripActivitySerializer,
    TripActivityStatusSerializer,
    TripActivityUpdateSerializer,
    TripActivityVoteSerializer,
    TripCalendarSyncStatusSerializer,
    TripCalendarSyncToggleSerializer,
    TripCommentCreateSerializer,
    TripCommentReactionSetSerializer,
    TripCommentSerializer,
    TripCreateSerializer,
    TripDetailSerializer,
    TripListQuerySerializer,
    TripMapQuerySerializer,
    TripMapResponseSerializer,
    TripMemberAddSerializer,
    TripMemberOrganizerSerializer,
    TripMemberSerializer,
    TripRsvpSerializer,
    TripSummarySerializer,
    TripUpdateSerializer,
    WhoAmISerializer,
)
from urbanlens.dashboard.external_api.throttling import TIER_READ, ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, PinReparentError, delete_pin, move_pin_to_coordinates, reparent_pin
from urbanlens.dashboard.services.pin_suggestions import LocationHit, attach_suggestion_photos, ingest_location_hits
from urbanlens.dashboard.services.pin_sync import InvalidSyncCursorError, StaleDeletedSinceError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.profile_settings import SettingsValidationError, apply_settings_patch, read_settings
from urbanlens.dashboard.services.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.trip_access import can_perform, get_trip_for_viewer, has_joined, is_organizer
from urbanlens.dashboard.services.trip_activities import (
    build_activity_rows,
    complete_activity,
    create_activity,
    delete_activity,
    set_activity_position,
    set_activity_rsvp,
    set_activity_status,
    set_activity_vote,
    update_activity,
)
from urbanlens.dashboard.services.trip_comments import add_comment, build_comment_tree, delete_comment, get_comment, set_comment_reaction
from urbanlens.dashboard.services.trip_crud import create_trip, delete_trip, update_trip
from urbanlens.dashboard.services.trip_errors import TripError, TripNotFoundError, TripPermissionError, TripValidationError
from urbanlens.dashboard.services.trip_map import build_trip_map_points
from urbanlens.dashboard.services.trip_membership import (
    add_member_by_username,
    join_trip,
    leave_trip,
    list_members,
    remove_member,
    require_trip_creator,
    resolve_trip_member,
    set_member_organizer,
    set_trip_rsvp,
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


# -- Trips ---------------------------------------------------------------------
#
# Every endpoint below delegates to the shared trip services
# (``services.trip_access``/``trip_crud``/``trip_membership``/``trip_activities``/
# ``trip_comments``/``trip_map``) - the same functions ``controllers.trip``
# calls. Nothing about permissions, quotas, share provenance, calendar-sync
# revocation, identity masking or location visibility is re-implemented here;
# a view that tried to would be a bug, because the two surfaces would then
# enforce different rules on the same data.
#
# Note that ``trips:read``/``trips:write`` are deliberately absent from
# ``account.model._default_api_key_scopes`` - trip data includes other members'
# identities, comments and coordinates, so reaching it requires an OAuth2 grant
# the user consented to by name, not a blanket PAT.


class TripErrorResponseMixin:
    """Maps the shared trip-service exceptions onto this API's error envelope.

    One table instead of a per-method ``except`` ladder, so a new endpoint
    cannot accidentally answer 500 for a condition every other endpoint already
    reports as 403 or 404.
    """

    #: Ordered most-specific-first; the first isinstance match wins.
    _TRIP_ERROR_STATUS: ClassVar[dict[type[TripError], int]] = {
        TripNotFoundError: 404,
        TripPermissionError: 403,
        TripValidationError: 400,
    }

    def error_response(self, exc: TripError) -> Response:
        """Answer a trip-service error with ``{"error": ...}`` and its mapped status.

        The message is emitted unescaped: it is a JSON string value, and the
        HTML escaping the internal HTMX surface applies would show up here as
        literal entities. ``TripMemberNotFoundError`` carries the raw submitted
        username for exactly this reason.

        Args:
            exc: The error raised by a trip service.

        Returns:
            The error response; anything unmapped falls back to 400.
        """
        status = next((code for cls, code in self._TRIP_ERROR_STATUS.items() if isinstance(exc, cls)), 400)
        return Response({"error": exc.message}, status=status)


class TripScopedApiView(TripErrorResponseMixin, ExternalApiView):
    """Base for the trip endpoints: resolves the trip once, for this caller.

    ``trips:read`` gates every read and ``trips:write`` every mutation; the
    tiered throttles pick the write bucket automatically from those scope
    names, so no endpoint here needs a ``throttle_tier_by_method`` override.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.TRIPS_READ}),
        "POST": frozenset({ApiKeyScope.TRIPS_WRITE}),
        "PATCH": frozenset({ApiKeyScope.TRIPS_WRITE}),
        "PUT": frozenset({ApiKeyScope.TRIPS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.TRIPS_WRITE}),
    }

    def trip(self, request: Request, trip_slug: str) -> Trip:
        """The trip identified by *trip_slug*, if the caller may see it.

        Args:
            request: The authenticated request.
            trip_slug: The trip's URL slug.

        Returns:
            The trip.

        Raises:
            TripNotFoundError: No such trip, or it isn't the caller's. Both
                answer 404 - see ``services.trip_access.get_trip_for_viewer``.
        """
        return get_trip_for_viewer(trip_slug, request.user.profile)


def _activity_place_fields(data: dict) -> dict[str, object]:
    """Translate the API's place fields into what ``resolve_activity_place`` reads.

    The shared resolver speaks the web form's vocabulary (``pin_slug``,
    ``location_uuid``, ``geocoded_lat``/``geocoded_lng``/``geocoded_name``).
    Rather than teach it a second one, the API's flatter field names are mapped
    here - so both surfaces resolve a place through identical code.

    Args:
        data: Validated activity payload.

    Returns:
        The keyword shape ``services.trip_activities.resolve_activity_place`` expects.
    """
    return {
        "pin_slug": data.get("pin_slug") or "",
        "location_uuid": data.get("location_uuid") or "",
        "geocoded_lat": "" if data.get("latitude") is None else str(data["latitude"]),
        "geocoded_lng": "" if data.get("longitude") is None else str(data["longitude"]),
        "geocoded_name": data.get("place_name") or "",
        "title": data.get("title") or "",
    }


def _has_place_fields(data: dict) -> bool:
    """Whether the caller submitted anything that would re-resolve the place.

    Args:
        data: Validated activity payload (presence-keyed for PATCH).

    Returns:
        True when at least one place field was present in the submission.
    """
    return any(key in data for key in ("pin_slug", "location_uuid", "latitude", "longitude", "place_name"))


def _calendar_sync_status(trip: Trip, profile: Profile) -> dict[str, object]:
    """Describe this caller's calendar mirroring for one trip.

    Args:
        trip: The trip in question.
        profile: The requesting profile.

    Returns:
        The dict :class:`TripCalendarSyncStatusSerializer` documents.
    """
    from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink

    account = GoogleCalendarAccount.objects.get_for_profile(profile)
    link = TripCalendarLink.objects.trip_level_link(trip, profile) if account else None
    return {
        "connected": account is not None,
        "linked": link is not None,
        "auto_sync": bool(link and link.auto_sync),
        "last_synced": link.last_synced if link else None,
    }


def _trip_viewer_block(trip: Trip, profile: Profile) -> dict[str, object]:
    """Describe what this specific caller may see and do on one trip.

    Args:
        trip: The trip in question.
        profile: The requesting profile.

    Returns:
        The dict :class:`TripViewerSerializer` documents.
    """
    membership = TripMembership.objects.for_trip_and_profile(trip, profile).first()
    return {
        "has_joined": has_joined(profile, trip),
        "is_organizer": is_organizer(profile, trip),
        "is_creator": trip.creator_id == profile.id,
        "membership_status": membership.status if membership else None,
        "rsvp": membership.rsvp if membership else None,
        "can_add_members": can_perform(profile, trip, trip.allow_add_members),
        "can_add_activities": can_perform(profile, trip, trip.allow_add_activities),
        "can_edit_activities": can_perform(profile, trip, trip.allow_edit_activities),
        "can_comment": can_perform(profile, trip, trip.allow_comments),
    }


def _trip_detail_payload(trip: Trip, profile: Profile) -> Trip:
    """Assemble the bundled detail payload for one trip.

    Decorates the instance in place rather than building a parallel dict, the
    same per-request pattern ``Trip.viewer_membership`` already uses (all three
    attributes are declared in the model's ``TYPE_CHECKING`` block).

    Args:
        trip: The trip to describe.
        profile: The requesting profile.

    Returns:
        The trip itself carrying ``viewer``, ``calendar_sync`` and ``members``,
        ready for :class:`TripDetailSerializer`.
    """
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities

    members = list_members(trip, profile)
    # The creator may not be one of the membership rows resolve_visible_identities
    # just masked, so mask that reference too rather than leaking a raw username.
    if trip.creator is not None and not any(m.profile_id == trip.creator_id for m in members):
        resolve_visible_identities(profile, [trip.creator])

    trip.viewer = _trip_viewer_block(trip, profile)
    trip.calendar_sync = _calendar_sync_status(trip, profile)
    trip.members = members
    return trip


class TripsView(TripErrorResponseMixin, PaginatedListMixin, ExternalApiView):
    """The caller's trips: GET lists them, POST creates one.

    Unlike ``pins/``, this is a browse endpoint rather than a delta-sync feed -
    a user has tens of trips, not thousands, and the app shows them in a paged
    list rather than reconciling them offline. See ``external_api.pagination``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.TRIPS_READ}),
        "POST": frozenset({ApiKeyScope.TRIPS_WRITE}),
    }

    @extend_schema(parameters=[TripListQuerySerializer], responses={200: TripSummarySerializer(many=True)})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's trips."""
        query = TripListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        profile = request.user.profile
        # for_list_page carries the same count annotations the web list page
        # uses, and returns a plain list for the "soonest first" ordering - so
        # it is materialized rather than paginated as a queryset.
        trips = list(Trip.objects.for_list_page(profile, sort=params["sort"], direction=params["dir"]))
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(trips, request, view=self)
        return paginator.get_paginated_response(TripSummarySerializer(page, many=True, context={"viewer": profile}).data)

    @extend_schema(request=TripCreateSerializer, responses={201: TripDetailSerializer, 200: TripDetailSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Create a trip owned by the caller, or replay an earlier create."""
        serializer = TripCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        try:
            trip, created = create_trip(
                profile,
                name=data.get("name"),
                description=data.get("description"),
                start_date=data.get("start_date"),
                end_date=data.get("end_date"),
                client_uuid=data.get("uuid"),
            )
        except TripError as exc:
            return self.error_response(exc)

        # 200 rather than 201 when `uuid` replayed an existing trip - the same
        # signal PinsView.post gives an offline outbox that it already landed.
        return Response(
            TripDetailSerializer(_trip_detail_payload(trip, profile), context={"viewer": profile}).data,
            status=201 if created else 200,
        )


class TripDetailView(TripScopedApiView):
    """One trip: GET it in full, PATCH its metadata, or DELETE it.

    GET bundles the roster, the caller's own standing, and calendar status, so
    the app's trip screen renders from a single request.
    """

    @extend_schema(responses={200: TripDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, trip_slug: str) -> Response:
        """Return one trip in full."""
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)
        return Response(TripDetailSerializer(_trip_detail_payload(trip, profile), context={"viewer": profile}).data)

    @extend_schema(request=TripUpdateSerializer, responses={200: TripDetailSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, trip_slug: str) -> Response:
        """Apply a partial update to one of the caller's trips."""
        serializer = TripUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = update_trip(self.trip(request, trip_slug), profile, changes=serializer.validated_data)
        except TripError as exc:
            return self.error_response(exc)
        return Response(TripDetailSerializer(_trip_detail_payload(trip, profile), context={"viewer": profile}).data)

    @extend_schema(responses={204: None, 403: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, trip_slug: str) -> Response:
        """Delete one of the caller's trips, stashing it for Undo History."""
        try:
            delete_trip(self.trip(request, trip_slug), request.user.profile)
        except TripError as exc:
            return self.error_response(exc)
        return Response(status=204)


class TripMapView(TripScopedApiView):
    """GET the trip's map markers.

    Deliberately unpaginated: a map has to fit its bounds to the whole set at
    once, and a client that only had the first page would draw the wrong
    viewport. The point set is bounded by ``max_trip_activities`` anyway.

    Returns ``services.trip_map.build_trip_map_points`` verbatim so it stays
    byte-identical to the web map's own ``map-data/`` payload.
    """

    @extend_schema(parameters=[TripMapQuerySerializer], responses={200: TripMapResponseSerializer, 404: ErrorSerializer})
    def get(self, request: Request, trip_slug: str) -> Response:
        """Return the trip's map markers for this caller."""
        query = TripMapQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)
        return Response({"points": build_trip_map_points(trip, request.user.profile, include_past=query.validated_data["include_past"])})


class TripJoinView(TripScopedApiView):
    """POST: accept an invitation to a trip, unlocking contribution rights."""

    @extend_schema(request=None, responses={200: TripDetailSerializer, 404: ErrorSerializer})
    def post(self, request: Request, trip_slug: str) -> Response:
        """Join the trip and return its refreshed detail."""
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            join_trip(trip, profile)
        except TripError as exc:
            return self.error_response(exc)
        return Response(TripDetailSerializer(_trip_detail_payload(trip, profile), context={"viewer": profile}).data)


class TripLeaveView(TripScopedApiView):
    """DELETE: leave a trip, or decline an invitation never accepted."""

    @extend_schema(responses={204: None, 400: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, trip_slug: str) -> Response:
        """Leave the trip; the creator is refused with 400."""
        try:
            leave_trip(self.trip(request, trip_slug), request.user.profile)
        except TripError as exc:
            return self.error_response(exc)
        return Response(status=204)


class TripRsvpView(TripScopedApiView):
    """PUT: set or clear the caller's trip-wide RSVP."""

    @extend_schema(request=TripRsvpSerializer, responses={200: TripDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, trip_slug: str) -> Response:
        """Persist the caller's RSVP and return the refreshed trip."""
        serializer = TripRsvpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            set_trip_rsvp(trip, profile, serializer.validated_data["rsvp"])
        except TripError as exc:
            return self.error_response(exc)
        return Response(TripDetailSerializer(_trip_detail_payload(trip, profile), context={"viewer": profile}).data)


class TripCalendarSyncView(TripScopedApiView):
    """POST: turn auto-sync on or off for an already-exported trip.

    Only the toggle, never the initial connection: establishing one needs
    Google's OAuth consent flow, which this surface does not build. When the
    response says ``connected: false``, the client should send the user to the
    web app to connect their calendar first - a 400 with the same status object
    tells it exactly that.
    """

    @extend_schema(
        request=TripCalendarSyncToggleSerializer,
        responses={200: TripCalendarSyncStatusSerializer, 400: TripCalendarSyncStatusSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, trip_slug: str) -> Response:
        """Set auto-sync for the caller's export link on this trip."""
        from urbanlens.dashboard.models.calendar_sync.model import TripCalendarLink

        serializer = TripCalendarSyncToggleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)

        link = TripCalendarLink.objects.trip_level_link(trip, profile)
        if link is None:
            # No link yet: nothing to toggle. The status object tells the client
            # whether the blocker is "no Google account" or "trip not exported".
            status = _calendar_sync_status(trip, profile)
            return Response(
                {**TripCalendarSyncStatusSerializer(status).data, "error": "Add this trip to your Google Calendar from the web app first."},
                status=400,
            )

        TripCalendarLink.objects.set_auto_sync(link.pk, serializer.validated_data["enabled"])
        return Response(TripCalendarSyncStatusSerializer(_calendar_sync_status(trip, profile)).data)


class TripMembersView(TripScopedApiView, PaginatedListMixin):
    """The trip's roster: GET lists it, POST invites someone by username."""

    @extend_schema(responses={200: TripMemberSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, trip_slug: str) -> Response:
        """Return one page of the trip's members, identities masked per viewer."""
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)
        return self.paginated_response(list_members(trip, request.user.profile), TripMemberSerializer, request)

    @extend_schema(
        request=TripMemberAddSerializer,
        responses={201: TripMemberSerializer, 200: TripMemberSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, trip_slug: str) -> Response:
        """Invite a user to the trip by username."""
        serializer = TripMemberAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            membership, created = add_member_by_username(trip, profile, serializer.validated_data["username"])
        except TripError as exc:
            return self.error_response(exc)

        resolve_visible_identities(profile, [membership.profile])
        # 200 on a re-invite: the membership already existed, so nothing was
        # created and no second notification was sent.
        return Response(TripMemberSerializer(membership).data, status=201 if created else 200)


class TripMemberDetailView(TripScopedApiView):
    """One member: PATCH their organizer flag, or DELETE them from the trip.

    Addressed by profile slug - or uuid, which is the only handle a caller has
    for a member whose identity their privacy settings mask. Either way the
    lookup never leaves this trip's roster, so it cannot be used to discover
    whether an arbitrary profile exists.
    """

    @extend_schema(
        request=TripMemberOrganizerSerializer,
        responses={200: TripMemberSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def patch(self, request: Request, trip_slug: str, member_slug: str) -> Response:
        """Set (not toggle) a member's organizer flag; creator only."""
        serializer = TripMemberOrganizerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            require_trip_creator(trip, profile)
            target = resolve_trip_member(trip, slug=member_slug)
            membership = set_member_organizer(trip, profile, target, is_organizer=serializer.validated_data["is_organizer"])
        except TripError as exc:
            return self.error_response(exc)

        resolve_visible_identities(profile, [membership.profile])
        return Response(TripMemberSerializer(membership).data)

    @extend_schema(responses={204: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, trip_slug: str, member_slug: str) -> Response:
        """Remove a member; members may remove themselves, else creator only."""
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            remove_member(trip, profile, resolve_trip_member(trip, slug=member_slug))
        except TripError as exc:
            return self.error_response(exc)
        return Response(status=204)


class TripActivitiesView(TripScopedApiView, PaginatedListMixin):
    """The trip's itinerary: GET lists it, POST adds a stop."""

    @extend_schema(parameters=[TripActivityListQuerySerializer], responses={200: TripActivitySerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, trip_slug: str) -> Response:
        """Return one page of the trip's activities."""
        query = TripActivityListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)
        # include_legs is off by default: each leg can cost a live OSRM routing
        # call, which a plain list fetch must never trigger.
        rows = build_activity_rows(trip, profile, include_legs=query.validated_data["include_legs"])
        return self.paginated_response(rows, TripActivitySerializer, request)

    @extend_schema(
        request=TripActivityCreateSerializer,
        responses={201: TripActivitySerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, trip_slug: str) -> Response:
        """Add one activity to the trip's itinerary."""
        serializer = TripActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            activity = create_activity(
                trip,
                profile,
                title=data.get("title"),
                notes=data.get("notes"),
                scheduled_at=data.get("scheduled_at"),
                scheduled_end=data.get("scheduled_end"),
                place=_activity_place_fields(data),
                child_trip_uuid=data.get("child_trip_uuid"),
                status=data.get("status"),
                location_hidden=data.get("location_hidden"),
            )
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_activity(trip, profile, activity.id), status=201)


def _serialize_one_activity(trip: Trip, profile: Profile, activity_id: int) -> dict:
    """Serialize a single activity through the shared render rows.

    Rebuilding the whole row set for one activity looks wasteful, but it is
    what guarantees the single-activity payload carries the same index, vote
    tallies, effective RSVP, ``can_manage`` and location-visibility decisions
    the list endpoint would give - the index in particular is only meaningful
    relative to its siblings.

    Args:
        trip: The trip owning the activity.
        profile: The requesting profile.
        activity_id: The activity to pick out.

    Returns:
        The serialized activity, or an empty dict when it is no longer present.
    """
    rows = build_activity_rows(trip, profile, include_legs=False)
    row = next((item for item in rows if item["activity"].id == activity_id), None)
    return TripActivitySerializer(row).data if row is not None else {}


class TripActivityDetailView(TripScopedApiView):
    """One activity: PATCH its fields, or DELETE it."""

    @extend_schema(
        request=TripActivityUpdateSerializer,
        responses={200: TripActivitySerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def patch(self, request: Request, trip_slug: str, activity_id: int) -> Response:
        """Apply a partial update to one activity."""
        serializer = TripActivityUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        changes: dict[str, object] = {key: data[key] for key in ("title", "notes", "scheduled_at", "scheduled_end", "status", "location_hidden", "child_trip_uuid") if key in data}
        if _has_place_fields(data):
            changes["place"] = _activity_place_fields(data)

        try:
            trip = self.trip(request, trip_slug)
            update_activity(trip, profile, activity_id, changes=changes)
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_activity(trip, profile, activity_id))

    @extend_schema(responses={204: None, 403: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, trip_slug: str, activity_id: int) -> Response:
        """Delete one activity from the trip."""
        try:
            delete_activity(self.trip(request, trip_slug), request.user.profile, activity_id)
        except TripError as exc:
            return self.error_response(exc)
        return Response(status=204)


class TripActivityPositionView(TripScopedApiView):
    """POST: save a map-drag position override for one activity.

    Requires the trip's edit-activities permission, and bounds-checks the
    coordinates. Both were missing from the endpoint this mirrors, which is
    now fixed on the internal surface too - see
    ``services.trip_activities.set_activity_position``.
    """

    @extend_schema(request=TripActivityPositionSerializer, responses={200: TripActivityPositionSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, trip_slug: str, activity_id: int) -> Response:
        """Persist the dragged coordinates and echo them back."""
        serializer = TripActivityPositionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            lat, lng = set_activity_position(
                self.trip(request, trip_slug),
                request.user.profile,
                activity_id,
                lat=serializer.validated_data["lat"],
                lng=serializer.validated_data["lng"],
            )
        except TripError as exc:
            return self.error_response(exc)
        return Response({"lat": lat, "lng": lng})


class TripActivityVoteView(TripScopedApiView):
    """PUT: set or clear the caller's vote on a proposed activity."""

    @extend_schema(request=TripActivityVoteSerializer, responses={200: TripActivitySerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, trip_slug: str, activity_id: int) -> Response:
        """Persist the caller's vote and return the refreshed activity."""
        serializer = TripActivityVoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            set_activity_vote(trip, profile, activity_id, vote=serializer.validated_data["vote"])
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_activity(trip, profile, activity_id))


class TripActivityStatusView(TripScopedApiView):
    """PUT: set an activity's status.

    ``completed`` routes to ``complete_activity`` rather than a plain status
    write, so completing through the API logs the same visit entries and date
    snapping the web app's "mark complete" does.
    """

    @extend_schema(request=TripActivityStatusSerializer, responses={200: TripActivitySerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, trip_slug: str, activity_id: int) -> Response:
        """Persist the requested status and return the refreshed activity."""
        serializer = TripActivityStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        status = serializer.validated_data["status"]
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            if status == TripActivity.STATUS_COMPLETED:
                complete_activity(trip, profile, activity_id)
            else:
                set_activity_status(trip, profile, activity_id, status=status)
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_activity(trip, profile, activity_id))


class TripActivityRsvpView(TripScopedApiView):
    """PUT: set or clear the caller's RSVP override for one activity."""

    @extend_schema(request=TripActivityRsvpSerializer, responses={200: TripActivitySerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, trip_slug: str, activity_id: int) -> Response:
        """Persist the override (null clears it) and return the refreshed activity."""
        serializer = TripActivityRsvpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            set_activity_rsvp(trip, profile, activity_id, rsvp=serializer.validated_data["rsvp"])
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_activity(trip, profile, activity_id))


class TripCommentsView(TripScopedApiView, PaginatedListMixin):
    """The trip's comments: GET the visible tree, POST a new comment."""

    @extend_schema(responses={200: TripCommentSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, trip_slug: str) -> Response:
        """Return one page of top-level comments, replies nested inline."""
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)

        rows = build_comment_tree(trip, profile)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(TripCommentSerializer(page, many=True, context={"viewer": profile}).data)

    @extend_schema(request=TripCommentCreateSerializer, responses={201: TripCommentSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, trip_slug: str) -> Response:
        """Post a comment (or a reply) on the trip."""
        serializer = TripCommentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            comment = add_comment(trip, profile, text=data["text"], parent_id=data.get("parent_id"))
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_comment(trip, profile, comment.id), status=201)


def _serialize_one_comment(trip: Trip, profile: Profile, comment_id: int) -> dict:
    """Serialize a single comment through the shared visible-comment tree.

    Going back through ``build_comment_tree`` rather than serializing the model
    directly is what applies the mention rendering, the author masking and the
    visibility gates - a comment serialized outside the tree would carry raw
    text and an unmasked author.

    Args:
        trip: The trip owning the comment.
        profile: The requesting profile.
        comment_id: The comment to pick out (top-level or a reply).

    Returns:
        The serialized comment, or an empty dict when it isn't visible.
    """
    rows = build_comment_tree(trip, profile)
    for row in rows:
        if row["comment"].id == comment_id:
            return TripCommentSerializer(row, context={"viewer": profile}).data
        for reply in row.get("replies", []):
            if reply["comment"].id == comment_id:
                return TripCommentSerializer(reply, context={"viewer": profile}).data
    return {}


class TripCommentDetailView(TripScopedApiView):
    """DELETE: remove a comment (its author, or the trip's creator)."""

    @extend_schema(responses={204: None, 403: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, trip_slug: str, comment_id: int) -> Response:
        """Delete one comment and any markup map attached to it."""
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            delete_comment(trip, profile, get_comment(trip, comment_id))
        except TripError as exc:
            return self.error_response(exc)
        return Response(status=204)


class TripCommentReactionsView(TripScopedApiView):
    """PUT: add or remove one of the caller's emoji reactions on a comment."""

    @extend_schema(
        request=TripCommentReactionSetSerializer,
        responses={200: TripCommentSerializer, 400: ErrorSerializer, 404: ErrorSerializer},
    )
    def put(self, request: Request, trip_slug: str, comment_id: int) -> Response:
        """Persist the target reaction state and return the refreshed comment."""
        serializer = TripCommentReactionSetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
            comment = get_comment(trip, comment_id)
            set_comment_reaction(comment, profile, data["emoji"], reacted=data["reacted"])
        except TripError as exc:
            return self.error_response(exc)
        return Response(_serialize_one_comment(trip, profile, comment_id))
