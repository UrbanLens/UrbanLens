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
from django.db.models import Count, Model
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
    LocationSearchQuerySerializer,
    LocationSearchResponseSerializer,
    PinAliasSerializer,
    PinCreateResponseSerializer,
    PinCreateSerializer,
    PinDetailSerializer,
    PinLinkCreateSerializer,
    PinLinkSerializer,
    PinNoteSerializer,
    PinSuggestionCreateResponseSerializer,
    PinSuggestionCreateSerializer,
    PinSyncQuerySerializer,
    PinSyncResponseSerializer,
    PinUpdateSerializer,
    PinVisitCreateSerializer,
    PinVisitSerializer,
    PlaceResolveResponseSerializer,
    PushDeviceRegisterSerializer,
    PushDeviceResponseSerializer,
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
    ExternalApiWriteThrottle,
    LocationSearchThrottle,
)
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.map_pins.autocomplete import resolve_google_place, search_google_places, search_local
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, PinReparentError, delete_pin, move_pin_to_coordinates, reparent_pin
from urbanlens.dashboard.services.pin_subresources import (
    AliasExistsError,
    AliasIsCurrentNameError,
    InvalidLinkError,
    PinSubResourceError,
    create_pin_alias,
    create_pin_link,
    create_pin_note,
    delete_pin_alias,
    delete_pin_link,
    delete_pin_note,
    promote_alias_to_name,
)
from urbanlens.dashboard.services.pin_suggestions import LocationHit, attach_suggestion_photos, ingest_location_hits
from urbanlens.dashboard.services.pin_sync import InvalidSyncCursorError, StaleDeletedSinceError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.profile_settings import SettingsValidationError, apply_settings_patch, read_settings
from urbanlens.dashboard.services.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.visits import VisitLoggingDisabledError, create_manual_visit, delete_visit, visit_logging_allowed
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet
    from rest_framework.request import Request
    from rest_framework.serializers import Serializer

logger = logging.getLogger(__name__)

#: The autocomplete sources ``locations/search/`` knows how to serve.
LOCATION_SEARCH_SOURCES = frozenset({"local", "places"})


def parse_search_sources(raw: str | None) -> frozenset[str]:
    """Parse the ``sources`` query param into the set of sources to search.

    Unknown entries are dropped rather than rejected: a newer client asking
    for a source this server doesn't have should still get the ones it does,
    instead of failing the whole search.

    Args:
        raw: The raw comma-separated value, or None when the param was absent.

    Returns:
        The recognized subset of :data:`LOCATION_SEARCH_SOURCES`. An absent
        param means all of them; an empty or wholly-unrecognized value means
        none, which yields an empty result rather than an implicit default.
    """
    if raw is None:
        return LOCATION_SEARCH_SOURCES
    requested = {part.strip().lower() for part in raw.split(",")}
    return frozenset(requested & LOCATION_SEARCH_SOURCES)

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


class OwnedPinMixin:
    """Pin lookup scoped to the requesting credential's owner.

    Every pin-scoped external endpoint resolves its pin through here, so
    another user's pin is uniformly indistinguishable from a nonexistent one -
    both 404, never 403, which would confirm the pin exists.
    """

    @staticmethod
    def _owned_pins(request: Request, pin_slug: str) -> QuerySet[Pin]:
        """Pins matching *pin_slug* (by slug or uuid) owned by the requesting user.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug, or its uuid.

        Returns:
            A queryset of at most one pin.
        """
        return Pin.objects.slug_or_uuid(pin_slug).filter(profile__user=request.user)

    def get_owned_pin(self, request: Request, pin_slug: str) -> Pin | None:
        """The key owner's pin, with every relation the detail payload reads.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug, or its uuid.

        Returns:
            The pin, or None when it doesn't exist or isn't the caller's.
        """
        return self._owned_pins(request, pin_slug).select_related("location", "profile", "parent_pin", "wiki", "cover_photo").first()

    def get_owned_pin_lite(self, request: Request, pin_slug: str) -> Pin | None:
        """The key owner's pin, without the detail-only joins.

        For sub-resource endpoints, which need the pin to scope and authorize
        the query rather than to serialize it - the extra joins would be
        loaded and thrown away.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug, or its uuid.

        Returns:
            The pin, or None when it doesn't exist or isn't the caller's.
        """
        return self._owned_pins(request, pin_slug).select_related("location", "profile").first()


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


class PinDetailView(OwnedPinMixin, ExternalApiView):
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

    @extend_schema(responses={200: PinDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return the key owner's full detail for one pin."""
        pin = self.get_owned_pin(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)
        return Response(build_pin_detail(pin, request.user.profile))

    @extend_schema(request=PinUpdateSerializer, responses={200: PinDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, pin_slug: str) -> Response:
        """Apply a partial update to one of the key owner's pins."""
        pin = self.get_owned_pin(request, pin_slug)
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
        pin = self.get_owned_pin(request, pin_slug)
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


#: Maps a sub-resource failure onto the status that describes it. Anything not
#: listed is a plain client error the caller can fix by changing the payload.
_SUBRESOURCE_ERROR_STATUS: dict[type[PinSubResourceError], int] = {
    AliasExistsError: 409,
    AliasIsCurrentNameError: 400,
    InvalidLinkError: 400,
}


def _subresource_error_status(exc: PinSubResourceError) -> int:
    """The HTTP status describing one sub-resource failure.

    Args:
        exc: The raised failure.

    Returns:
        The status to answer with; 400 for anything unmapped.
    """
    return _SUBRESOURCE_ERROR_STATUS.get(type(exc), 400)


class PinSubResourceView[SubResourceT: Model](OwnedPinMixin, PaginatedListMixin, ExternalApiView):
    """Base for a pin's list-and-create sub-resource collections.

    Notes, aliases, and links are structurally the same endpoint: a
    pin-owned, CASCADE-deleted child collection that lists (paginated) and
    creates. Only the related name, the serializers, and which service
    function performs the create differ, so subclasses declare those four
    things and inherit the ownership check, the 404 shape, the pagination, and
    the error-to-status mapping.

    Visits deliberately do *not* use this base - creating one is gated on the
    owner's visit-tracking preference and answers 403, which has no analogue
    here.
    """

    #: The ``related_name`` of the collection on ``Pin``.
    related_name: ClassVar[str] = ""
    #: Ordering for the list. Must be total, or pages overlap and drop rows.
    ordering: ClassVar[tuple[str, ...]] = ("pk",)
    output_serializer: ClassVar[type[Serializer]]
    input_serializer: ClassVar[type[Serializer]]

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    def get_queryset(self, pin: Pin) -> QuerySet:
        """The pin's sub-resource rows, in a deterministic order.

        Args:
            pin: The owning pin.

        Returns:
            The ordered collection.
        """
        return getattr(pin, self.related_name).order_by(*self.ordering)

    def serializer_context(self, pin: Pin) -> dict[str, object]:
        """Extra context the output serializer needs.

        Args:
            pin: The owning pin.

        Returns:
            Context passed to the output serializer; empty by default.
        """
        return {}

    def create(self, pin: Pin, data: dict) -> SubResourceT:
        """Create one row from validated input.

        Args:
            pin: The owning pin.
            data: The input serializer's validated data.

        Returns:
            The created row.

        Raises:
            NotImplementedError: Always, unless a subclass overrides this.
        """
        raise NotImplementedError

    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the pin's sub-resource rows."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)
        return self.paginated_response(self.get_queryset(pin), self.output_serializer, request, context=self.serializer_context(pin))

    def post(self, request: Request, pin_slug: str) -> Response:
        """Validate the payload and add one row to the pin."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        serializer = self.input_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            created = self.create(pin, serializer.validated_data)
        except PinSubResourceError as exc:
            return Response({"error": str(exc)}, status=_subresource_error_status(exc))

        return Response(self.output_serializer(created, context=self.serializer_context(pin)).data, status=201)


class PinSubResourceDetailView[SubResourceT: Model](OwnedPinMixin, ExternalApiView):
    """Base for deleting one row of a pin's sub-resource collection."""

    #: The ``related_name`` of the collection on ``Pin``.
    related_name: ClassVar[str] = ""
    #: The URL keyword carrying the row's id.
    lookup_kwarg: ClassVar[str] = ""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    def perform_delete(self, pin: Pin, obj: SubResourceT) -> None:
        """Delete one row through the service that owns its side effects.

        Args:
            pin: The owning pin.
            obj: The row to delete.

        Raises:
            NotImplementedError: Always, unless a subclass overrides this.
        """
        raise NotImplementedError

    @extend_schema(responses={204: None, 400: ErrorSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, pin_slug: str, **kwargs: int) -> Response:
        """Delete one of the pin's sub-resource rows."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        obj: SubResourceT | None = getattr(pin, self.related_name).filter(pk=kwargs[self.lookup_kwarg]).first()
        if obj is None:
            return Response({"error": "No such item."}, status=404)

        try:
            self.perform_delete(pin, obj)
        except PinSubResourceError as exc:
            return Response({"error": str(exc)}, status=_subresource_error_status(exc))
        return Response(status=204)


class PinNotesView(PinSubResourceView[PinNote]):
    """The pin's personal notes: GET lists them, POST adds one.

    Notes are private to the pin's owner and append-only - there is no update
    endpoint; a client edits by deleting and re-adding.
    """

    related_name = "notes"
    ordering = ("-created", "-pk")
    output_serializer = PinNoteSerializer
    input_serializer = PinNoteSerializer

    def create(self, pin: Pin, data: dict) -> PinNote:
        """Add one note to the pin."""
        return create_pin_note(pin, text=data["text"])

    @extend_schema(responses={200: PinNoteSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the pin's notes, newest first."""
        return super().get(request, pin_slug)

    @extend_schema(request=PinNoteSerializer, responses={201: PinNoteSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Add one note to the pin."""
        return super().post(request, pin_slug)


class PinNoteDetailView(PinSubResourceDetailView[PinNote]):
    """DELETE: remove one of the pin's personal notes."""

    related_name = "notes"
    lookup_kwarg = "note_id"

    def perform_delete(self, pin: Pin, obj: PinNote) -> None:
        """Delete the note."""
        delete_pin_note(obj)


class PinAliasesView(PinSubResourceView[PinAlias]):
    """The pin's alternate names: GET lists them, POST adds one.

    The alias list is the full set of names the pin is known by, *including*
    its current one - which is flagged by ``is_current`` rather than omitted.
    """

    related_name = "aliases"
    ordering = ("name", "pk")
    output_serializer = PinAliasSerializer
    input_serializer = PinAliasSerializer

    def serializer_context(self, pin: Pin) -> dict[str, object]:
        """Hand the serializer the pin, so ``is_current`` costs no per-row query."""
        return {"pin": pin}

    def create(self, pin: Pin, data: dict) -> PinAlias:
        """Add one alternate name to the pin."""
        return create_pin_alias(pin, name=data["name"], kind=data["kind"])

    @extend_schema(responses={200: PinAliasSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the pin's alternate names."""
        return super().get(request, pin_slug)

    @extend_schema(request=PinAliasSerializer, responses={201: PinAliasSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Add one alternate name; a duplicate (case-insensitively) answers 409."""
        return super().post(request, pin_slug)


class PinAliasDetailView(PinSubResourceDetailView[PinAlias]):
    """DELETE: remove one of the pin's alternate names.

    Deleting the pin's *current* name is refused with 400 - the alias list is
    defined to always contain it, so the pin must be renamed first.
    """

    related_name = "aliases"
    lookup_kwarg = "alias_id"

    def perform_delete(self, pin: Pin, obj: PinAlias) -> None:
        """Delete the alias, recording a tombstone so nothing recreates it."""
        delete_pin_alias(pin, obj)


class PinAliasUseView(OwnedPinMixin, ExternalApiView):
    """POST: make one of the pin's aliases its current name.

    Answers with the full pin detail rather than the alias, so a client can
    apply the rename (and everything derived from it) from the identical
    payload shape ``GET pins/{slug}/`` already hands it.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(request=None, responses={200: PinDetailSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str, alias_id: int) -> Response:
        """Promote the alias to the pin's name and return the updated pin."""
        pin = self.get_owned_pin(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        alias = pin.aliases.filter(pk=alias_id).first()
        if alias is None:
            return Response({"error": "No such alias."}, status=404)

        promote_alias_to_name(pin, alias)
        return Response(build_pin_detail(pin, request.user.profile))


class PinLinksView(PinSubResourceView[PinLink]):
    """The pin's external links: GET lists them, POST adds one."""

    related_name = "links"
    ordering = ("order", "pk")
    output_serializer = PinLinkSerializer
    input_serializer = PinLinkCreateSerializer

    def create(self, pin: Pin, data: dict) -> PinLink:
        """Add one external link to the pin."""
        return create_pin_link(pin, name=data.get("name", ""), url=data["url"])

    @extend_schema(responses={200: PinLinkSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the pin's external links."""
        return super().get(request, pin_slug)

    @extend_schema(request=PinLinkCreateSerializer, responses={201: PinLinkSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Add one external link; a non-http(s) url answers 400."""
        return super().post(request, pin_slug)


class PinLinkDetailView(PinSubResourceDetailView[PinLink]):
    """DELETE: remove one of the pin's external links."""

    related_name = "links"
    lookup_kwarg = "link_id"

    def perform_delete(self, pin: Pin, obj: PinLink) -> None:
        """Delete the link, recording a tombstone so no plugin recreates it."""
        delete_pin_link(pin, obj)


class PinVisitsView(OwnedPinMixin, PaginatedListMixin, ExternalApiView):
    """The pin's visit history: GET lists it, POST logs a visit.

    Its own view rather than a :class:`PinSubResourceView` subclass: logging a
    visit is gated on the owner's visit-tracking preference (403 when off,
    rather than silently discarding it), and reads/writes here are scoped to
    ``visits:*`` rather than ``pins:*`` so a client can be granted a pin's
    contents without its owner's movement history.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.VISITS_READ}),
        "POST": frozenset({ApiKeyScope.VISITS_WRITE}),
    }

    @extend_schema(responses={200: PinVisitSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the pin's visits, most recent first."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        # Annotated rather than counted per row - a page of visits would
        # otherwise cost one extra query each.
        visits = pin.visit_history.annotate(photo_count=Count("images")).order_by("-visited_at", "-pk")
        return self.paginated_response(visits, PinVisitSerializer, request)

    @extend_schema(request=PinVisitCreateSerializer, responses={201: PinVisitSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Log a manual visit to the pin."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        serializer = PinVisitCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            visit = create_manual_visit(pin, visited_at=data["visited_at"], notes=data.get("notes"))
        except VisitLoggingDisabledError as exc:
            return Response({"error": str(exc)}, status=403)

        # Re-read through the same annotation the list path uses, so the created
        # row carries photo_count rather than the response shape depending on
        # which endpoint produced it.
        created = pin.visit_history.annotate(photo_count=Count("images")).get(pk=visit.pk)
        return Response(PinVisitSerializer(created).data, status=201)


class PinVisitDetailView(OwnedPinMixin, ExternalApiView):
    """DELETE: remove one of the pin's logged visits."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.VISITS_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, pin_slug: str, visit_id: int) -> Response:
        """Delete the visit and re-derive the pin's last-visited date."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        visit = pin.visit_history.filter(pk=visit_id).first()
        if visit is None:
            return Response({"error": "No such visit."}, status=404)

        delete_visit(visit)
        return Response(status=204)


class LocationSearchView(ExternalApiView):
    """GET: autocomplete over the caller's own pins and, optionally, external places.

    The same two sources the web map's search bar uses
    (``MapController.autocomplete_local`` / ``autocomplete_places``), merged
    into one call so a mobile client makes one request per keystroke instead
    of two. Results reuse ``AutocompleteResult.to_dict()`` verbatim, so both
    surfaces stay on one wire shape.

    External place lookups are skipped - and flagged with ``places_disabled``
    rather than silently returning fewer results - when the caller has turned
    external lookups off, or when no places provider is configured.
    """

    #: Replaces the shared read cap; see LocationSearchThrottle.
    throttle_classes = [ExternalApiBurstThrottle, LocationSearchThrottle]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
    }

    @extend_schema(parameters=[LocationSearchQuerySerializer], responses={200: LocationSearchResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return autocomplete suggestions for the submitted query."""
        serializer = LocationSearchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        query = params["q"].strip()
        # Matches the web autocomplete's floor: a single character matches so
        # much of a large pin set that the result is noise, and the query is
        # expensive enough not to run for it.
        if len(query) < 2:
            return Response({"results": [], "places_disabled": False})

        sources = parse_search_sources(params.get("sources"))
        profile = request.user.profile
        results = list(search_local(query, profile)) if "local" in sources else []

        places_disabled = False
        if "places" in sources:
            api_key = settings.google_unrestricted_api_key
            redata_configured = bool(settings.redata_api_url and settings.redata_api_key)
            if not profile.external_apis_enabled or not (api_key or redata_configured):
                places_disabled = True
            else:
                results.extend(search_google_places(query, api_key or ""))

        return Response({"results": [result.to_dict() for result in results[: params["limit"]]], "places_disabled": places_disabled})


class PlaceResolveView(ExternalApiView):
    """GET: resolve an autocomplete ``place_id`` to coordinates.

    Split from the search call on purpose: coordinates cost a Places Details
    lookup per place, so they're fetched only for the one suggestion the user
    actually picks rather than for every row shown.
    """

    #: Charged against the autocomplete budget - this is the second half of a
    #: single search interaction, not general-purpose reading.
    throttle_classes = [ExternalApiBurstThrottle, LocationSearchThrottle]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
    }

    @extend_schema(responses={200: PlaceResolveResponseSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 503: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Resolve the submitted ``place_id`` to coordinates and a name."""
        place_id = (request.query_params.get("place_id") or "").strip()
        if not place_id:
            return Response({"error": "A place_id is required."}, status=400)

        profile = request.user.profile
        # The internal MapController.resolve_place omits this gate even though
        # its own autocomplete_places applies it - so a user who turned
        # external lookups off can still trigger a Places Details call by
        # selecting a suggestion. Recorded in docs/PROBLEMS.md; this surface
        # does not reproduce the omission.
        if not profile.external_apis_enabled:
            return Response({"error": "External lookups are turned off in your settings."}, status=403)

        api_key = settings.google_unrestricted_api_key
        redata_configured = bool(settings.redata_api_url and settings.redata_api_key)
        if not (api_key or redata_configured):
            return Response({"error": "No places provider is configured."}, status=503)

        latitude, longitude, name = resolve_google_place(place_id, api_key or "")
        if latitude is None or longitude is None:
            return Response({"error": "That place could not be resolved."}, status=404)

        return Response({"lat": latitude, "lng": longitude, "name": name or ""})


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
