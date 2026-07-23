"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope
from urbanlens.dashboard.external_api.serializers import (
    ErrorSerializer,
    PinCreateResponseSerializer,
    PinCreateSerializer,
    PinSuggestionCreateResponseSerializer,
    PinSuggestionCreateSerializer,
    PinSyncQuerySerializer,
    PinSyncResponseSerializer,
    PushDeviceRegisterSerializer,
    PushDeviceResponseSerializer,
    TombstoneSyncQuerySerializer,
    TombstoneSyncResponseSerializer,
    WhoAmISerializer,
)
from urbanlens.dashboard.external_api.throttling import ApiKeyRateThrottle
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pin_suggestions import LocationHit, attach_suggestion_photos, ingest_location_hits
from urbanlens.dashboard.services.pin_sync import InvalidSyncCursorError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.visits import visit_logging_allowed

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
    throttle_classes = [ApiKeyRateThrottle]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {}

    @property
    def required_scopes(self) -> frozenset[ApiKeyScope]:
        """The scopes the current request's HTTP method requires."""
        return self.required_scopes_by_method.get(self.request.method or "", frozenset())


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

    @extend_schema(parameters=[TombstoneSyncQuerySerializer], responses={200: TombstoneSyncResponseSerializer, 400: ErrorSerializer})
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
