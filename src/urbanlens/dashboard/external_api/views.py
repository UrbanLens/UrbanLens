"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any, ClassVar, overload
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Model
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.errors import ErrorEnvelopeMixin
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope, credential_grants, filter_sources_by_grants
from urbanlens.dashboard.external_api.serializers import (
    AuthSessionSerializer,
    ErrorSerializer,
    FriendInviteResponseSerializer,
    FriendInviteSerializer,
    FriendListQuerySerializer,
    FriendListResponseSerializer,
    FriendMuteSerializer,
    FriendRequestCreateSerializer,
    FriendshipSerializer,
    JournalEntrySerializer,
    JournalResponseSerializer,
    LabelCustomizationSerializer,
    LabelMergeResponseSerializer,
    LabelMergeSerializer,
    LabelQuerySerializer,
    LabelSerializer,
    LabelWriteSerializer,
    LocationSearchQuerySerializer,
    LocationSearchResponseSerializer,
    MemoriesTimelineQuerySerializer,
    MemoryEventSerializer,
    NotificationListQuerySerializer,
    NotificationListResponseSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
    OnThisDayResponseSerializer,
    PhotoFileSerializer,
    PhotoLabelsSerializer,
    PhotoListQuerySerializer,
    PhotoListResponseSerializer,
    PhotoSerializer,
    PhotoUploadSerializer,
    PhotoVoteResponseSerializer,
    PhotoVoteSerializer,
    PinAliasSerializer,
    PinCreateResponseSerializer,
    PinCreateSerializer,
    PinDetailSerializer,
    PinLinkCreateSerializer,
    PinLinkSerializer,
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
    PinNoteSerializer,
    PinSuggestionCreateResponseSerializer,
    PinSuggestionCreateSerializer,
    PinSuggestionListResponseSerializer,
    PinSyncQuerySerializer,
    PinSyncResponseSerializer,
    PinUpdateSerializer,
    PinVisitCreateSerializer,
    PinVisitSerializer,
    PlaceResolveResponseSerializer,
    ProfileDetailSerializer,
    ProfileNoteSerializer,
    ProfileNoteWriteSerializer,
    ProfileUpdateSerializer,
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
    SafetyMapListResponseSerializer,
    SafetyMapSerializer,
    SafetyPartnerInviteSerializer,
    SafetyPhotoAttachSerializer,
    SafetyPhotoListResponseSerializer,
    SafetyPhotoSerializer,
    SafetyPreferenceSerializer,
    SavedFilterSerializer,
    SavedFilterUpdateResponseSerializer,
    SavedFilterWriteSerializer,
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
    LocationSearchThrottle,
)
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.labels.meta import DEFAULT_LABEL_COLOR
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.profile.model import _COMMUNITY_GATED_VISIBILITY_FIELDS, Profile
from urbanlens.dashboard.models.profile.note import ProfileNote
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinPartner, SafetyCheckinStatus, SafetyPreference
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.core.colors import InvalidColorError, require_color
from urbanlens.dashboard.services.labels.customization import clear_label_customization, upsert_label_customization
from urbanlens.dashboard.services.labels.hierarchy import would_create_cycle
from urbanlens.dashboard.services.labels.merge import LabelMergeError, merge_labels
from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label, label_conflict_message
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.map_pins.autocomplete import resolve_google_place, search_google_places, search_local
from urbanlens.dashboard.services.media.images import delete_stored_file
from urbanlens.dashboard.services.media.media_labels import MediaLabelError, set_media_labels
from urbanlens.dashboard.services.media.media_relevance import toggle_media_vote
from urbanlens.dashboard.services.memories.aggregator import BBox, get_memory_events
from urbanlens.dashboard.services.memories.journal import get_journal_entries
from urbanlens.dashboard.services.memories.photos import create_pin_and_log_visit, log_visit_on_pin
from urbanlens.dashboard.services.notifications.notification_center import (
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
from urbanlens.dashboard.services.notifications.push import PushRegistrationError, register_device, unregister_device
from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo
from urbanlens.dashboard.services.pins.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile
from urbanlens.dashboard.services.pins.pin_detail import build_pin_detail
from urbanlens.dashboard.services.pins.pin_edit import (
    ORGANIZE_LABEL_KINDS,
    PinEditError,
    PinHasChildrenError,
    PinMoveError,
    PinReparentError,
    apply_pin_edits,
    delete_pin,
    move_pin_to_coordinates,
    reparent_pin,
)
from urbanlens.dashboard.services.pins.pin_list_membership import (
    add_pins_to_list,
    remove_pins_from_list,
    reorder_list_items,
    resync_lists_for_saved_filter,
    resync_smart_list,
)
from urbanlens.dashboard.services.pins.pin_subresources import (
    AliasExistsError,
    AliasIsCurrentNameError,
    InvalidLinkError,
    LinkExistsError,
    PinSubResourceError,
    create_pin_alias,
    create_pin_link,
    create_pin_note,
    delete_pin_alias,
    delete_pin_link,
    delete_pin_note,
    promote_alias_to_name,
)
from urbanlens.dashboard.services.pins.pin_suggestions import LocationHit, accept_pin_suggestion, attach_suggestion_photos, ingest_location_hits, pending_suggestions_for_profile, reject_pin_suggestion
from urbanlens.dashboard.services.pins.pin_sync import InvalidSyncCursorError, StaleDeletedSinceError, sync_pins_page, sync_tombstones_page
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identities, resolve_visible_identity
from urbanlens.dashboard.services.profile.profile_annotations import get_annotations
from urbanlens.dashboard.services.profile.profile_settings import SettingsValidationError, apply_settings_patch, read_settings
from urbanlens.dashboard.services.search.filter_criteria import CriteriaOwnershipError, validate_criteria_ownership
from urbanlens.dashboard.services.social.friendship import (
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
    unmute_profile,
)
from urbanlens.dashboard.services.trips.trip_access import can_perform, get_trip_for_viewer, has_joined, is_organizer
from urbanlens.dashboard.services.trips.trip_activities import (
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
from urbanlens.dashboard.services.trips.trip_comments import add_comment, build_comment_tree, delete_comment, get_comment, set_comment_reaction
from urbanlens.dashboard.services.trips.trip_crud import create_trip, delete_trip, update_trip
from urbanlens.dashboard.services.trips.trip_errors import TripError, TripNotFoundError, TripPermissionError, TripValidationError
from urbanlens.dashboard.services.trips.trip_map import build_trip_map_points
from urbanlens.dashboard.services.trips.trip_membership import (
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
from urbanlens.dashboard.services.undo.handlers.pin_list import MODEL_LABEL as PIN_LIST_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.visits.safety import (
    CheckinArchivedError,
    SafetyValidationError,
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
from urbanlens.dashboard.services.visits.visits import (
    VisitInFutureError,
    VisitLoggingDisabledError,
    accept_visit_suggestion,
    create_manual_visit,
    delete_visit,
    reject_visit_suggestion,
    sync_last_visited,
    visit_logging_allowed,
)
from urbanlens.dashboard.services.wiki.wiki_access import wikis_hidden_by_pin_move
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Callable

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


@overload
def _validated_color(data: dict, *, default: str, key: str = "color") -> str: ...


@overload
def _validated_color(data: dict, *, default: None = None, key: str = "color") -> str | None: ...


def _validated_color(data: dict, *, default: str | None = None, key: str = "color") -> str | None:
    """Read a colour from a request body, 400ing rather than substituting.

    `clean_color` replaces a value it does not recognise with the default, which
    is right for a form post (the user sees the swatch that resulted) and wrong
    here: the client is told the write succeeded and only finds out by reading
    the record back. Missing and blank keep falling back - "unset" is not an
    invalid colour.

    Args:
        data: The parsed request body.
        default: What a missing or blank value falls back to.
        key: The body key to read.

    Returns:
        A validated colour, or `default` - a `str` when `default` is one, so a
        result assigned into a non-nullable column needs no further narrowing.

    Raises:
        ValidationError: When the key is present and is not a colour. Rendered
            by `ErrorEnvelopeMixin` as the package's field-keyed 400.
    """
    try:
        return require_color(data.get(key), default=default)
    except InvalidColorError as exc:
        raise ValidationError({key: [str(exc)]}) from exc


class ExternalApiView(ErrorEnvelopeMixin, APIView):
    """Base for every external endpoint: credential auth, scope gate, per-credential throttle.

    Two credential kinds are accepted - PAT-style ``ApiKey`` bearer keys and
    django-oauth-toolkit access tokens (the native apps' OAuth2 + PKCE flow) -
    both enforced against the same per-method scope declarations.

    Scopes are declared per HTTP method in ``required_scopes_by_method``;
    ``HasApiKeyScope`` reads the ``required_scopes`` property and fails closed
    when the current method has no entry, so an endpoint can never gain a new
    method without also declaring what that method requires.

    ``ErrorEnvelopeMixin`` is listed first so its ``get_exception_handler``
    beats ``APIView``'s in the MRO. Inheriting it here rather than per-endpoint
    is what makes ``{"error": ...}`` the package's *only* error shape: without
    it, a handler's hand-written returns use that envelope while the 400 from
    ``is_valid(raise_exception=True)`` and every 401/403/404/405/429 DRF raises
    on the way in use ``detail``/field-keyed shapes instead, and no generated
    client can parse all three. See ``external_api.errors``.
    """

    authentication_classes = [ApiKeyAuthentication, OAuth2Authentication]
    permission_classes = [HasApiKeyScope]
    #: All three apply together: the burst cap counts every request, while the
    #: read and write caps each count only their own tier (see ``throttling``).
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {}

    def initial(self, request, *args, **kwargs):
        """Authenticate, then bind the caller as the source of any writes.

        ``WriteSourceMiddleware`` cannot do this for the API. Both credential
        kinds here are DRF authenticators, resolved in this method - so at
        middleware time a bearer-token request still carries an
        ``AnonymousUser``, and every write from a native app would record with
        no actor at all. Field provenance decides what a concealed viewer sees
        of their *own* contributions, so losing the identity here would conceal
        an API editor's edit from the API editor.

        Bound for the life of the request rather than in a context manager: DRF
        dispatches the handler after ``initial()`` returns, so there is no block
        to wrap. The ContextVar is per request-thread and every entry point
        rebinds before its first write.
        """
        super().initial(request, *args, **kwargs)

        from urbanlens.dashboard.models.abstract.versioning import WriteSource, bind_write_source

        user = getattr(request, "user", None)
        profile_id = getattr(getattr(user, "profile", None), "pk", None) if user is not None and user.is_authenticated else None
        if profile_id is None:
            bind_write_source(WriteSource.SYSTEM)
        else:
            bind_write_source(WriteSource.USER, actor=profile_id)

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
    """GET: the calling API key's owner - their profile uuid and slug, nothing else.

    Still the narrowest *profile* read in the API: no settings, friends, or any
    other private data, per the ``profile:read`` scope's definition. The slug is
    served alongside the uuid because it is the identifier every other endpoint
    in this API actually speaks - see :class:`WhoAmISerializer` for why a client
    cannot recognize itself in other endpoints' payloads without it.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PROFILE_READ}),
    }

    @extend_schema(responses=WhoAmISerializer)
    def get(self, request: Request) -> Response:
        """Return the authenticated key owner's profile uuid and slug."""
        profile = request.user.profile
        # Backfilled rather than read straight off the row: profiles created
        # before slugs existed still have an empty one, and this endpoint
        # promising a slug that is sometimes "" would make every client write
        # a fallback path for it. ensure_slug() persists what it generates, so
        # the slug handed out here is the same one the profile routes answer to.
        profile.ensure_slug()
        return Response(WhoAmISerializer(profile).data)


class PinsView(ExternalApiView):
    """The key owner's pins: GET delta-syncs them, POST creates one.

    GET is a sync feed, not a browse API: ordered by ``(updated, pk)``, it
    pages through pins changed since ``modified_since`` with an opaque cursor
    and hands back the ``sync_watermark`` to use as the next sync's
    ``modified_since``. Deletions are the separate ``pins/deleted/`` feed.

    POST goes through the exact same ``services.pins.pin_creation.create_pin_for_profile``
    call as the map UI's "Add pin" form - the same sanitization, geocoding
    gate, and background enrichment apply regardless of which caller created
    the pin. A caller-generated ``uuid`` makes the create idempotent for
    offline-outbox retries.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(operation_id="pins_list", parameters=[PinSyncQuerySerializer], responses={200: PinSyncResponseSerializer, 400: ErrorSerializer})
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
            return Response({"error": exc.safe_message}, status=400)

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
                color=_validated_color(data),
                description=data.get("description"),
                pin_type=data.get("pin_type"),
                client_uuid=data.get("uuid"),
                parent_id=data.get("parent_id"),
                name_is_user_provided=data.get("name_is_user_provided", False),
            )
        except PinCreationForbiddenError as exc:
            return Response({"error": exc.safe_message}, status=403)
        except PinCreationError as exc:
            return Response({"error": exc.safe_message}, status=400)

        pin = result.pin
        parent_pin = pin.parent_pin
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
                "parent_uuid": str(parent_pin.uuid) if parent_pin else None,
            },
            status=201 if result.created else 200,
        )


class PinDetailView(OwnedPinMixin, ExternalApiView):
    """GET the key owner's full pin detail; PATCH or DELETE it.

    GET returns a superset of the sync feed's payload - description, dates,
    security indicators, personal notes/aliases/links, custom fields, the
    property boundary, the cover photo, and the discovered wiki slug (see
    ``services.pins.pin_detail.build_pin_detail``).

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

    @extend_schema(
        request=PinUpdateSerializer,
        responses={200: PinDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer},
        description=(
            "Partially update one of your own pins. Every field is optional: an absent field is left untouched, "
            "an explicit null clears it. `label_uuids` is a full replacement of the pin's tag/category/status "
            "labels, not a delta.\n\n"
            "**Side effect worth surfacing to your users:** writing `priority`, `danger` or `vulnerability` is not "
            "purely a private edit. When the pin is attached to a community wiki and the owner has the matching "
            "wiki-sync setting on, the new value is published as their community WikiStatVote on that wiki, where it "
            "feeds the composite score everyone with access to that wiki sees. Setting the value back to 0 withdraws "
            "the vote.\n\n"
            "`rating` is not accepted here - a pin's rating is a Review, written through PUT/DELETE "
            "/pins/{pin_slug}/review/. `address`, `city`, `state` and `country` are derived from the shared Location "
            "the pin points at and are read-only; move a pin by sending latitude/longitude instead."
        ),
    )
    def patch(self, request: Request, pin_slug: str) -> Response:
        """Apply a partial update to one of the key owner's pins.

        Field writes go through ``services.pins.pin_edit.apply_pin_edits``, the same
        function behind the website's own pin-edit dialog, so the two surfaces
        cannot drift on which companion flags a write implies (a submitted
        ``pin_type`` marking the type user-provided, for instance) or on the
        tombstones a label removal has to leave behind.

        A move that would cost the owner access to a community wiki (wiki
        visibility follows where their pins are) is refused with 409 and a
        ``requires_wiki_loss_confirmation`` payload naming them, matching the
        internal ``PinViewSet``. Re-send with ``confirm_wiki_loss: true`` to go
        ahead. Every other update is unaffected, as is a move that costs the
        owner nothing.

        Args:
            request: The authenticated request carrying the partial update.
            pin_slug: The pin's slug, or its uuid.

        Returns:
            The pin's full detail payload, or an error envelope: 404 when the
            pin isn't the caller's (never 403 - that would confirm it exists),
            400 for an unresolvable parent/label or an impossible move, 409 for
            the unconfirmed wiki-loss handshake.
        """
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

        # Same reasoning for labels: an unknown uuid is refused before anything
        # is written. Scoped to labels this profile may actually use, so another
        # user's private label is "no such label" rather than a usable id - and
        # a partial resolution is a 400, never a silently smaller set than the
        # client asked for.
        labels: list[Label] | None = None
        if "label_uuids" in data:
            wanted = list(dict.fromkeys(data["label_uuids"]))
            labels = list(Label.objects.visible_to(pin.profile).filter(uuid__in=wanted, kind__in=ORGANIZE_LABEL_KINDS))
            if len(labels) != len(wanted):
                return Response({"error": "One or more label_uuids do not name a label you can use."}, status=400)

        # Asked only once the request is known to be otherwise valid: confirming
        # a move and then being handed a 400 for an unrelated bad field would be
        # a pointless prompt.
        if "latitude" in data and not data.get("confirm_wiki_loss"):
            lost = wikis_hidden_by_pin_move(pin, data["latitude"], data["longitude"])
            if lost:
                return Response(
                    {
                        "error": "This move would end your access to a community wiki.",
                        "requires_wiki_loss_confirmation": True,
                        "wikis": [{"name": wiki.name, "slug": wiki.location.slug} for wiki in lost],
                    },
                    status=409,
                )

        # The nested `security` object is flattened into the flat Pin-column
        # mapping by the serializer itself - parsing the wire format is its job,
        # and the `security` wire key collides with a Pin column of the same
        # name, which is exactly the kind of trap that must be solved in one
        # place. See PinUpdateSerializer.pin_field_edits.
        edits = serializer.pin_field_edits()

        try:
            with transaction.atomic():
                if "latitude" in data:
                    move_pin_to_coordinates(pin, data["latitude"], data["longitude"])

                apply_pin_edits(pin, edits, labels=labels, visited=data.get("visited"))

                if "parent_id" in data:
                    # Raises on failure - propagating out of the atomic block rolls
                    # back every change already applied above.
                    reparent_pin(pin, new_parent)
        except (PinEditError, PinMoveError, PinReparentError) as exc:
            return Response({"error": exc.safe_message}, status=400)

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
    ``services.pins.pin_suggestions.ingest_location_hits``) that the key's owner
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
            return Response({"error": exc.safe_message}, status=400)
        except StaleDeletedSinceError as exc:
            # 410 Gone: tombstones this old may already be pruned, so the
            # incremental deletions feed can no longer be trusted from that
            # point. The client must full-resync (walk pins/ without
            # modified_since and drop local pins absent from the result).
            return Response({"error": exc.safe_message, "full_resync_required": True}, status=410)

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
            return Response({"error": exc.safe_message}, status=400)

        return Response(PushDeviceResponseSerializer(device).data, status=201)


class AccountSettingsView(ExternalApiView):
    """GET the caller's account preferences; PATCH to change them.

    Named for the account rather than matching ``controllers.settings.SettingsView``
    (the site's own multi-form settings page) - the two are unrelated and share
    only the underlying ``Profile`` fields, via
    ``services.profile.profile_settings``.

    PATCH is partial by construction: only submitted keys are touched, so a
    client syncing one toggle never overwrites preferences changed on the web
    in the meantime. The response is always the full post-save document, since
    ``Profile.save()`` may coerce community-gated fields and the client needs
    to see what it actually ended up with.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SETTINGS_READ}),
        # Both scopes: the response is always the full document (see below),
        # which a write-only credential has no business reading.
        "PATCH": frozenset({ApiKeyScope.SETTINGS_WRITE, ApiKeyScope.SETTINGS_READ}),
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
        return Image.objects.filter(uuid=image_uuid, profile__user=request.user).select_related("pin", "wiki", "wiki__location", "visit", "location", "profile", "direct_message").prefetch_related("labels").first()


def _resolve_own_pin(request: Request, value: str) -> Pin | None:
    """Resolve a pin slug-or-uuid against the caller's own pins only."""
    return Pin.objects.slug_or_uuid(value).filter(profile__user=request.user).select_related("location").first()


class PhotosView(PaginatedListMixin, ExternalApiView):
    """The key owner's photo library: GET browses it, POST uploads to it.

    GET is a browse endpoint (page-number paginated), not a delta sync: unlike
    ``pins/`` there is no tombstone feed for photos, so a client that needs to
    detect deletions re-walks the list.

    POST runs the identical admission pipeline as the Memories page's
    drag-and-drop uploader (``services.photos.photo_upload.upload_photo``) - the same
    media-type sniffing, feature gates, malware/size checks, duplicate
    rejection and storage quota.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }
    parser_classes = [MultiPartParser]

    @extend_schema(operation_id="photos_list", parameters=[PhotoListQuerySerializer], responses={200: PhotoListResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the key owner's own photos."""
        serializer = PhotoListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        profile = request.user.profile

        # Deliberately NOT .photos(): PhotoSerializer/build_photo_payload return
        # media_type for exactly this reason - this endpoint (and its POST,
        # which already runs the same media-type-agnostic upload_photo()) is a
        # general media library, "photos" being the API's noun for it rather
        # than a literal restriction. Narrowing this to actual photos would be
        # a real regression for any client listing their videos/documents.
        #
        # profile__user: Profile.username reads self.user.username.
        # pin__location(__wiki): pin_name -> Pin.effective_name -> Location.display_name,
        # which reads the location's Wiki. Both are per-row queries without these.
        queryset = Image.objects.uploaded_by(profile).select_related(
            "pin",
            "pin__location",
            "pin__location__wiki",
            "wiki",
            "wiki__location",
            "visit",
            "location",
            "profile",
            "profile__user",
            "direct_message",
        )
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

        from urbanlens.dashboard.services.memories.photos import pending_suggestion_image_ids

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        images = list(page or [])
        # One suggestion query for the page instead of one per photo inside
        # classify_photo, matching how the Memories queue builds its cards.
        pending_ids = pending_suggestion_image_ids(images)
        payload = [build_photo_payload(image, profile, pending_ids) for image in images]
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
            visit = PinVisit.objects.filter(pk=data["visit"], pin__profile=profile).select_related("pin__location").first()
            if visit is None:
                return Response({"error": "No such visit."}, status=400)
            # The two references have to agree. Each passed its own ownership
            # check independently, so a `pin` of A plus a `visit` belonging to
            # B was accepted and stored verbatim - a photo that shows in A's
            # gallery while claiming it was taken on a visit to B, which
            # quietly breaks both gallery filtering and visit history.
            if pin is not None and visit.pin_id != pin.pk:
                return Response({"error": "That visit belongs to a different pin."}, status=400)
            # A visit implies its pin, so a caller naming only the visit gets
            # the association filled in rather than an unfiled photo.
            if pin is None:
                pin = visit.pin

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
            image = Image.objects.visible_to(profile).filter(uuid=image_uuid).select_related("pin", "wiki", "wiki__location", "visit", "location", "profile", "direct_message").prefetch_related("labels").first()
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        return Response(PhotoSerializer(build_photo_payload(image, profile)).data)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, image_uuid: UUID) -> Response:
        """Delete one of the caller's own photos, file included.

        A photo the caller contributed to a community wiki is taken off their
        own library and left on the wiki, unless ``?from_wiki=true`` says
        otherwise - the same rule the pin gallery follows. Contributing is a
        deliberate act, so undoing it is another one, and a client that says
        nothing gets the answer that needs no action. Clients can ask first:
        ``wiki_slug`` and ``source`` are both on the photo payload.

        ``from_wiki`` is honoured only for an upload. A photo fetched from a URL
        was a public resource online before this app saw it, so there is no
        consent here to withdraw - it is removed on the wiki itself or not at
        all, and that is enforced here rather than left to the client.

        Args:
            request: The API request; ``from_wiki=true`` also withdraws it.
            image_uuid: UUID of the photo.

        Returns:
            204 when something was removed, 404 for a photo that is not the
            caller's.
        """
        image = self._get_image(request, image_uuid)
        if image is None:
            # 404 rather than 403 for someone else's photo - the same
            # no-oracle policy the rest of this API and the media gate follow.
            return Response({"error": "No such photo."}, status=404)

        withdrawing = request.query_params.get("from_wiki", "").lower() in {"1", "true", "yes"} and image.is_own_contribution
        if image.wiki_id is not None and not withdrawing:
            Image.objects.filter(pk=image.pk).update(pin=None)
            return Response(status=204)

        # Reached only when there's no wiki copy to protect, or the caller
        # explicitly asked to withdraw it too - either way nothing is left that
        # should keep this row alive, regardless of any pin still attached.
        # Matches controllers.vault_photos.PhotoActionView.delete_photo: drop the
        # stored file before the row, so deleting the row can't orphan bytes -
        # delete_stored_file has its own reference-count check for a file
        # shared with another row (e.g. pin-to-pin sharing).
        delete_stored_file(image)
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
            return Response({"error": exc.safe_message}, status=400)

        image.refresh_from_db()
        return Response(PhotoSerializer(build_photo_payload(image, profile)).data)


class PhotoVoteView(_OwnedImageMixin, ExternalApiView):
    """POST: cast, flip, or withdraw a community relevance vote on a photo.

    Only meaningful for a photo materialized into a Location's Media gallery -
    a plain personal upload has no ``(source, item_key)`` identity for
    ``MediaRelevance`` to key a vote by, and is refused with 400.

    **Resolves by visibility, not ownership** - the one write in this group
    that does, and deliberately so. A relevance vote does not mutate the image:
    it inserts the *caller's own* ``MediaRelevance`` row keyed by
    ``(source, item_key, profile)``, which is why the reasoning in
    :class:`_OwnedImageMixin` (a write must not reach a photo you can merely
    look at) does not apply here. Voting only on your own uploads is not
    community voting at all, and a gallery photo belonging to someone else has
    no other endpoint through which a client could reach it - the wiki gallery
    surfaces it, ``PhotoDetailView.get`` serves it, and this route answered 404.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    @extend_schema(request=PhotoVoteSerializer, responses={200: PhotoVoteResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, image_uuid: UUID) -> Response:
        """Record the caller's vote and return the item's new net score."""
        # Same two-step widening PhotoDetailView.get uses: the caller's own
        # photo first, then anything visible_to() admits.
        image = self._get_image(request, image_uuid)
        if image is None:
            image = Image.objects.visible_to(request.user.profile).filter(uuid=image_uuid).select_related("location", "profile").first()
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


class PinSuggestionListApiView(ExternalApiView):
    """GET: the caller's pending batch-scan pin suggestions.

    Distinct from ``PinSuggestionsView`` (POST-only - stages a *new*
    suggestion submitted by an external "discovery" app): this lists the
    review queue an Immich library sweep or local-folder scan already
    populated, mirroring ``VisitSuggestionsView`` for the sibling suggestion
    type. Not paginated, matching that sibling - a review queue is naturally
    small and bounded by how much a batch scan found.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(responses={200: PinSuggestionListResponseSerializer})
    def get(self, request: Request) -> Response:
        """List every pending suggestion in the caller's batch-scan review queue."""
        profile = request.user.profile
        suggestions = pending_suggestions_for_profile(profile).select_related("pin").order_by("-created")

        payload = [
            {
                "id": suggestion.pk,
                "status": suggestion.status,
                "origin": suggestion.origin,
                "is_new_pin": suggestion.is_new_pin,
                "pin_slug": suggestion.pin.slug if suggestion.pin else None,
                "pin_name": suggestion.pin.effective_name if suggestion.pin else None,
                "latitude": suggestion.latitude,
                "longitude": suggestion.longitude,
                "hit_count": suggestion.hit_count,
                "visit_dates": suggestion.visit_dates,
                "suggested_name": suggestion.suggested_name,
                "suggested_description": suggestion.suggested_description,
                "suggested_pin_type": suggestion.suggested_pin_type,
                "suggested_aliases": suggestion.suggested_aliases,
                "suggested_links": suggestion.suggested_links,
                "created": suggestion.created,
            }
            for suggestion in suggestions
        ]
        return Response(PinSuggestionListResponseSerializer({"suggestions": payload}).data)


class PinSuggestionActionApiView(ExternalApiView):
    """POST: accept or reject one pending pin suggestion.

    Applies the suggestion's own defaults - its ``suggested_name`` for a
    brand-new pin, no label or candidate-photo selection. The web review
    queue's richer accept dialog (name override, label picker, candidate
    Immich/local-scan photo picker) is not mirrored here in this pass; see
    ``docs/notes/mobile_app_notes.md``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PHOTOS_WRITE}),
    }

    # request=None: the action is carried entirely by the URL, so there is no
    # body for drf-spectacular to infer a request serializer from.
    @extend_schema(request=None, responses={204: None, 404: ErrorSerializer})
    def post(self, request: Request, suggestion_id: int, action: str) -> Response:
        """Apply ``accept`` or ``reject`` to one of the caller's pending suggestions."""
        if action not in {"accept", "reject"}:
            return Response({"error": "No such action."}, status=404)

        profile = request.user.profile
        suggestion = PinSuggestion.objects.filter(pk=suggestion_id, profile=profile, status=PinSuggestionStatus.PENDING).first()
        if suggestion is None:
            return Response({"error": "No such suggestion."}, status=404)

        if action == "reject":
            reject_pin_suggestion(suggestion)
        else:
            accept_pin_suggestion(suggestion, profile)
        return Response(status=204)


class MemoriesTimelineView(PaginatedListMixin, ExternalApiView):
    """GET: one page of the caller's Memories timeline - routes, trips, visits, photos.

    Defaults to the trailing 90 days, matching the internal Memories page's
    own default window - a full history is never loaded from a single
    request. Wraps ``services.memories.aggregator.get_memory_events``, the
    same data the internal page's map/timeline renders.
    """

    #: Mirrors ``controllers.memories._DEFAULT_WINDOW_DAYS``.
    _DEFAULT_WINDOW_DAYS = 90

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(parameters=[MemoriesTimelineQuerySerializer], responses={200: MemoryEventSerializer(many=True), 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of MemoryEvents for the requested date range/viewport."""
        serializer = MemoriesTimelineQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        today = timezone.now().date()
        start = data.get("start") or today - timedelta(days=self._DEFAULT_WINDOW_DAYS)
        end = data.get("end") or today

        bbox = None
        raw_bbox = data.get("bbox")
        if raw_bbox:
            try:
                min_lat, min_lng, max_lat, max_lng = (float(part) for part in raw_bbox.split(","))
            except ValueError:
                bbox = None
            else:
                bbox = BBox(min_lat, min_lng, max_lat, max_lng)

        events = get_memory_events(request.user.profile, start, end, bbox=bbox)
        return self.paginated_response(events, MemoryEventSerializer, request)


class MemoriesOnThisDayApiView(ExternalApiView):
    """GET: past-year visits/routes/photos matching today's month/day.

    Mirrors the internal Memories page's "on this day" callout, including its
    cap of ``_ON_THIS_DAY_LIMIT`` rows per category - a sensible default for a
    naturally small, date-scoped result rather than a fully paginated feed.
    """

    _ON_THIS_DAY_LIMIT = 10

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(responses={200: OnThisDayResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return this month/day's past-year visits, routes, and photos."""
        from urbanlens.dashboard.services.geo.geo import geometry_to_geojson

        profile = request.user.profile
        today = timezone.now().date()

        visits = PinVisit.objects.filter(pin__profile=profile, visited_at__month=today.month, visited_at__day=today.day).exclude(visited_at__year=today.year).select_related("pin").order_by("-visited_at")[: self._ON_THIS_DAY_LIMIT]
        routes = Route.objects.for_profile(profile).filter(started_at__month=today.month, started_at__day=today.day).exclude(started_at__year=today.year).order_by("-started_at")[: self._ON_THIS_DAY_LIMIT]
        photos = (
            Image.objects.filter(profile=profile, taken_at__month=today.month, taken_at__day=today.day)
            .exclude(taken_at__year=today.year)
            .select_related("pin", "wiki", "wiki__location", "profile", "location", "visit", "direct_message")
            .prefetch_related("labels")
            .order_by("-taken_at")[: self._ON_THIS_DAY_LIMIT]
        )

        payload = {
            "today": today.isoformat(),
            "visits": [{"pin_slug": visit.pin.slug, "pin_name": visit.pin.effective_name, "visited_at": visit.visited_at, "notes": visit.notes} for visit in visits],
            "routes": [{"uuid": route.uuid, "name": route.name, "started_at": route.started_at, "distance_meters": route.distance_meters, "path": geometry_to_geojson(route.path)} for route in routes],
            "photos": [build_photo_payload(image, profile) for image in photos],
        }
        return Response(OnThisDayResponseSerializer(payload).data)


class MemoriesJournalView(PaginatedListMixin, ExternalApiView):
    """GET: the caller's Memories journal - visit notes, ratings, comments, article edits.

    The journal is an aggregate of four separate privacy domains, so it is
    filtered per source rather than gated by a single scope - the same
    partial-fulfilment contract global search and the undo feed use. See
    :data:`JOURNAL_SOURCE_SCOPES`.
    """

    #: Every scope a credential must hold before the matching journal source is
    #: included, keyed by ``services.memories.journal.JOURNAL_SOURCES`` key.
    #:
    #: Serving the whole feed on ``photos:read`` alone would be a scope
    #: escalation rather than a convenience: the entries carry complete visit
    #: notes, pin/wiki/trip comment bodies, ratings, and - when a revision has
    #: no edit summary - the full text of private pin and wiki articles. Those
    #: are exactly the payloads ``visits:read``, ``pins:read``, ``trips:read``
    #: and ``wiki:read`` exist to gate, so a photos-only integration could read
    #: all of them by asking the journal instead of the domain endpoint.
    #:
    #: Each entry lists ``PHOTOS_READ`` (the endpoint's own scope) *as well as*
    #: its domain scope, per ``filter_sources_by_grants``' contract: a section
    #: must never be granted on the strength of a check made elsewhere.
    #:
    #: ``comments`` requires ``pins:read`` *and* ``trips:read`` because the one
    #: source yields pin, wiki and trip comments interleaved and cannot be
    #: split without three separate queries; ``wiki:read`` joins them for the
    #: wiki comments it also carries. Requiring all three is the strict
    #: reading, and the strict reading is the correct default for a source that
    #: cannot be subdivided.
    JOURNAL_SOURCE_SCOPES: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "visits": frozenset({ApiKeyScope.PHOTOS_READ, ApiKeyScope.VISITS_READ}),
        "reviews": frozenset({ApiKeyScope.PHOTOS_READ, ApiKeyScope.PINS_READ}),
        "comments": frozenset({ApiKeyScope.PHOTOS_READ, ApiKeyScope.PINS_READ, ApiKeyScope.WIKI_READ, ApiKeyScope.TRIPS_READ}),
        "articles": frozenset({ApiKeyScope.PHOTOS_READ, ApiKeyScope.PINS_READ, ApiKeyScope.WIKI_READ}),
    }

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(responses={200: JournalResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's journal, newest first.

        Uses the external API's standard page-number envelope - see
        ``PaginatedListMixin`` - rather than a bespoke ``offset``/``limit``/
        ``total`` shape, which could never gain a field later without breaking
        clients.
        """
        grants = filter_sources_by_grants(request.auth, self.JOURNAL_SOURCE_SCOPES)

        # get_journal_entries materializes every selected source in full - that
        # is the existing internal behavior (the Memories page renders the whole
        # feed), so pagination is applied to the resulting list rather than
        # pushed into the service, which would mean paginating four
        # heterogeneous querysets and merging them.
        entries = get_journal_entries(request.user.profile, sources=grants.granted)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(entries, request, view=self)
        response = paginator.get_paginated_response(JournalEntrySerializer(page, many=True).data)
        # Named so a client can tell "nothing happened yet" from "your
        # credential cannot see this kind of entry" and prompt for
        # re-authorization - see SourceGrants.
        response.data["omitted_sources"] = list(grants.omitted)
        return response


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
                return Response({"error": exc.safe_message}, status=400)

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
        # A bare save() writes every column from this request's snapshot,
        # silently reverting any field a concurrent request changed in
        # between - including one made through PinListEditView, the other
        # independent implementation of this same partial-update logic.
        changed_fields: set[str] = set()

        if "name" in data:
            if PinList.objects.for_profile(profile).filter(name=data["name"]).exclude(pk=pin_list.pk).exists():
                return Response({"error": "You already have a list with that name."}, status=400)
            pin_list.name = data["name"]
            changed_fields.add("name")
        if "description" in data:
            pin_list.description = data["description"]
            changed_fields.add("description")
        if "is_smart" in data:
            pin_list.is_smart = data["is_smart"]
            changed_fields.add("is_smart")
        if "smart_boundary" in data:
            pin_list.smart_boundary = data["smart_boundary"]
            changed_fields.add("smart_boundary")
        if "smart_filter" in data:
            pin_list.smart_filter = data["smart_filter"]
            changed_fields.add("smart_filter")
        if "source_saved_filter_uuid" in data:
            pin_list.source_saved_filter = source_filter
            changed_fields.add("source_saved_filter")
            # Pointing a list at a filter copies that filter's criteria in;
            # detaching it (null) leaves the last snapshot in place, matching
            # PinListEditView and the SET_NULL on the FK itself.
            if source_filter is not None:
                pin_list.smart_filter = source_filter.criteria
                changed_fields.add("smart_filter")

        if pin_list.smart_filter is not None:
            try:
                validate_criteria_ownership(pin_list.smart_filter, profile)
            except CriteriaOwnershipError as exc:
                return Response({"error": exc.safe_message}, status=400)

        if changed_fields:
            pin_list.save(update_fields=[*changed_fields, "updated"])

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
        stash_for_undo(PIN_LIST_MODEL_LABEL, [pin_list], request.user.profile)
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
            return Response({"error": exc.safe_message}, status=400)

        if SavedFilter.objects.name_taken_for(profile, data["name"]):
            return Response({"error": "You already have a saved filter with that name."}, status=400)

        saved_filter = SavedFilter.objects.create(
            profile=profile,
            name=data["name"],
            icon=data.get("icon") or "bookmark",
            color=_validated_color(data, default=""),
            opacity=data.get("opacity", 100),
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
                return Response({"error": exc.safe_message}, status=400)

        if "name" in data:
            if SavedFilter.objects.name_taken_for(profile, data["name"], exclude_pk=saved_filter.pk):
                return Response({"error": "You already have a saved filter with that name."}, status=400)
            saved_filter.name = data["name"]
        if "icon" in data:
            saved_filter.icon = data["icon"] or "bookmark"
        if "color" in data:
            saved_filter.color = _validated_color(data, default="")
        if "opacity" in data:
            saved_filter.opacity = data["opacity"]
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

        queryset = Label.objects.visible_to(profile).with_customizations_for(profile).in_display_order()
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

        # Same check the HTML form path runs, for the same reason: without it the
        # unique constraint raises IntegrityError and the client gets a 500 where
        # a 400 explaining the collision is what it can act on.
        conflict = find_conflicting_label(profile=profile, name=data["name"], kind=data["kind"])
        if conflict is not None:
            return Response({"error": label_conflict_message(conflict, singular_title=data["kind"].title())}, status=409)

        label = Label.objects.create(
            profile=profile,
            name=data["name"],
            description=data.get("description") or None,
            kind=data["kind"],
            color=_validated_color(data, default=DEFAULT_LABEL_COLOR),
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

        # A bare save() writes every column from this request's snapshot,
        # silently reverting any field a concurrent request changed in
        # between - including one made through LabelEditView, the other
        # independent implementation of this same partial-update logic.
        changed_fields: list[str] = []

        if "name" in data:
            conflict = find_conflicting_label(profile=profile, name=data["name"], kind=label.kind, exclude_pk=label.pk)
            if conflict is not None:
                return Response({"error": label_conflict_message(conflict, singular_title=label.kind.title())}, status=409)
            label.name = data["name"]
            changed_fields.append("name")
        if "description" in data:
            label.description = data.get("description") or None
            changed_fields.append("description")
        if "color" in data:
            label.color = _validated_color(data)
            changed_fields.append("color")
        if "icon" in data:
            label.icon = data.get("icon") or None
            changed_fields.append("icon")
        if "order" in data:
            label.order = data["order"]
            changed_fields.append("order")
        if "allow_auto_tag" in data:
            label.allow_auto_tag = data["allow_auto_tag"]
            changed_fields.append("allow_auto_tag")
        if "keywords" in data:
            label.keywords = data.get("keywords") or None
            changed_fields.append("keywords")

        # Validated before anything is written. Saving first and checking the
        # hierarchy afterwards meant a PATCH combining an ordinary field with a
        # cycle-forming `parent_uuids` persisted the ordinary field and *then*
        # answered 400 - a rejected request that had already been half applied,
        # which no client can reason about or undo.
        parent_ids = [parent.pk for parent in parents]
        if "parent_uuids" in data and would_create_cycle(label, parent_ids):
            return Response({"error": "That parent would create a loop in the label hierarchy."}, status=400)

        # `kind` is deliberately ignored on update - see LabelWriteSerializer.
        if changed_fields:
            label.save(update_fields=changed_fields)
            # A label's icon/color/name feed into every pin's cached map marker
            # without touching the Pin row itself, so the client's cache-
            # freshness check (keyed to Max(Pin.updated)) would otherwise never
            # notice this change - same reasoning as LabelEditView's internal
            # equivalent, missing here before this fix.
            Pin.objects.filter(profile=profile, labels=label).update(updated=timezone.now())

        if "parent_uuids" in data:
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
            color=_validated_color(data),
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

        requested_uuids = serializer.validated_data["source_uuids"]
        sources = list(Label.objects.filter(uuid__in=requested_uuids, profile=profile))
        if not sources:
            return Response({"error": "No labels to merge."}, status=400)

        # All-or-nothing. Filtering by owner silently dropped any uuid that was
        # unknown or belonged to someone else, so a stale or malformed batch
        # merged and *deleted* whichever sources happened to resolve and then
        # reported success - an irreversible partial application of a
        # destructive operation the caller believes ran in full. Anything the
        # caller named that can't be merged fails the whole request instead.
        unresolved = {str(value) for value in requested_uuids} - {str(source.uuid) for source in sources}
        if unresolved:
            return Response({"error": f"No such label(s): {', '.join(sorted(unresolved))}."}, status=404)

        # Captured before the merge - the source rows are gone afterwards.
        merged_uuids = [str(source.uuid) for source in sources]
        try:
            result = merge_labels(target=target, sources=sources, profile=profile)
        except LabelMergeError as exc:
            return Response({"error": exc.safe_message}, status=400)

        return Response(
            {
                "target": LabelSerializer(_reload_label(target, profile)).data,
                "merged_uuids": merged_uuids,
                "pins_moved": result.pins_moved,
            }
        )


#: Maps a sub-resource failure onto the status that describes it. Anything not
#: listed is a plain client error the caller can fix by changing the payload.
_SUBRESOURCE_ERROR_STATUS: dict[type[PinSubResourceError], int] = {
    AliasExistsError: 409,
    AliasIsCurrentNameError: 400,
    InvalidLinkError: 400,
    LinkExistsError: 409,
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
            return Response({"error": exc.safe_message}, status=_subresource_error_status(exc))

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
            return Response({"error": exc.safe_message}, status=_subresource_error_status(exc))
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
            return Response({"error": exc.safe_message}, status=403)
        except VisitInFutureError as exc:
            # Normally unreachable - PinVisitCreateSerializer rejects a future
            # time first, with field-level detail. Mapped anyway so the service
            # guard cannot surface as a 500 if the two ever disagree.
            return Response({"error": exc.safe_message}, status=400)

        # Re-read through the same annotation the list path uses, so the created
        # row carries photo_count rather than the response shape depending on
        # which endpoint produced it.
        created = pin.visit_history.annotate(photo_count=Count("images")).get(pk=visit.pk)
        return Response(PinVisitSerializer(created).data, status=201)


class PinVisitDetailView(OwnedPinMixin, ExternalApiView):
    """PATCH: edit one of the pin's logged visits. DELETE: remove it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PATCH": frozenset({ApiKeyScope.VISITS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.VISITS_WRITE}),
    }

    @extend_schema(request=PinVisitCreateSerializer, responses={200: PinVisitSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, pin_slug: str, visit_id: int) -> Response:
        """Update the visit's date and/or notes, then re-derive the pin's last-visited date.

        Only the two fields the create endpoint itself accepts (``visited_at``,
        ``notes``) are writable here - participants, photos, and the drawn map
        snapshot stay the web dialog's concern, so there is only ever one write
        path for each of those.
        """
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        visit = pin.visit_history.filter(pk=visit_id).first()
        if visit is None:
            return Response({"error": "No such visit."}, status=404)

        serializer = PinVisitCreateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        update_fields = []
        if "visited_at" in data:
            visit.visited_at = data["visited_at"]
            update_fields.append("visited_at")
        if "notes" in data:
            visit.notes = data["notes"]
            update_fields.append("notes")
        if update_fields:
            visit.save(update_fields=[*update_fields, "updated"])
            sync_last_visited(pin)

        updated = pin.visit_history.annotate(photo_count=Count("images")).get(pk=visit.pk)
        return Response(PinVisitSerializer(updated).data)

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
        # selecting a suggestion. Recorded under "Messaging / external API (noted
        # 2026-07-26)" in docs/PROBLEMS.md; this surface
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

    @extend_schema(operation_id="safety_checkins_list", responses={200: SafetyCheckinListResponseSerializer})
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
        except SafetyValidationError as exc:
            # An already-active check-in for this scope is a state conflict, not a
            # malformed request - a mobile client must be able to tell the two
            # apart to offer "open the existing one" instead of "fix your input".
            return Response({"error": exc.safe_message}, status=409)

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
            return Response({"error": exc.safe_message}, status=409)

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
        if not check_in(checkin, request.user.profile):
            # Lost a race with another resolution between the check above and
            # the conditional UPDATE - same outcome as the fast path.
            return Response({"error": "This check-in has already been resolved."}, status=409)
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
        if not cancel_checkin(checkin):
            # Lost a race with another resolution between the check above and
            # the conditional UPDATE - same outcome as the fast path.
            return Response({"error": "This check-in has already been resolved."}, status=409)
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
        except SafetyValidationError as exc:
            # The service's messages are already user-facing and specific
            # (unknown username, self-invite, blocked, already invited, cap
            # reached); reused verbatim so the app and the web UI say the same thing.
            return Response({"error": exc.safe_message}, status=400)
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
        # was only checked at connect time - see services.visits.safety.remove_checkin_partner.
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
        delete_stored_file(image)
        image.delete()
        return Response(status=204)


class SafetyCheckinMapsView(SafetyCheckinScopedView, PaginatedListMixin):
    """GET: the check-in's maps. POST: attach one of the caller's own maps."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyMapListResponseSerializer, 404: ErrorSerializer})
    def get(self, request: Request, checkin_slug: str) -> Response:
        """Return the check-in's primary route map plus every attached reference map."""
        checkin = self._get_checkin(request, checkin_slug)
        if checkin is None:
            return self._not_found()
        maps: list[dict] = []
        if checkin.markup_map is not None:
            maps.append({"uuid": str(checkin.markup_map.uuid), "title": checkin.markup_map.title, "is_primary": True})
        maps += [{"uuid": str(m.uuid), "title": m.title, "is_primary": False} for m in checkin.markup_maps.all()]
        return self.paginated_response(maps, SafetyMapSerializer, request)

    @extend_schema(request=SafetyMapAttachSerializer, responses={200: SafetyMapListResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
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
            # Same rule the web attach view enforces: services.visits.safety._build_archive_payload
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
        ``services.visits.safety.save_contact_defaults`` deletes and recreates the whole
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


def _friend_identity(viewer: Profile, subject: Profile, *, visible_pks: set[int] | None = None) -> dict[str, Any]:
    """Shape ``subject`` for ``FriendProfileSerializer`` as ``viewer`` may see them.

    Always routed through ``services.profile.identity_visibility.resolve_visible_identity``
    rather than read off the model, so a profile whose privacy settings don't
    permit ``viewer`` is masked here exactly as it is in the web UI. The
    ``uuid`` is still returned when masked - it is an opaque handle the caller
    needs in order to act on the relationship, and it discloses no identity.

    Args:
        viewer: The profile doing the looking.
        subject: The profile being displayed.
        visible_pks: Pre-resolved ``Profile.visible_profile_pks`` when several
            subjects are serialized together, so the visibility lookup runs once
            for the page rather than once per row.

    Returns:
        A dict matching ``FriendProfileSerializer``'s fields.
    """
    identity = resolve_visible_identity(viewer, subject, visible_pks=visible_pks)
    masked = identity["is_masked"]
    return {
        "uuid": subject.uuid,
        "username": identity["display_name"],
        "slug": None if masked else subject.slug,
        "avatar_url": identity["display_avatar_url"],
        "is_masked": masked,
    }


def _serialize_friendship(viewer: Profile, friendship: Friendship, *, visible_pks: set[int] | None = None) -> dict[str, Any]:
    """Shape one ``Friendship`` from ``viewer``'s point of view.

    ``status`` and ``relationship_type`` are passed through untouched so the
    wire values stay the model's own capitalized strings.

    Args:
        viewer: The profile the relationship is being described to.
        friendship: The relationship row.
        visible_pks: Pre-resolved ``Profile.visible_profile_pks`` when a page of
            relationships is serialized together.

    Returns:
        A dict matching ``FriendshipSerializer``'s fields.
    """
    outgoing = friendship.from_profile_id == viewer.pk
    other = friendship.to_profile if outgoing else friendship.from_profile
    return {
        "profile": _friend_identity(viewer, other, visible_pks=visible_pks),
        "status": friendship.status,
        "relationship_type": friendship.relationship_type,
        "direction": "outgoing" if outgoing else "incoming",
        "message": friendship.request_message,
        "is_muted": friendship.is_muted_by(viewer),
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
            return Response({"error": exc.safe_message}, status=400)

        # Resolved once for the page: _friend_identity masks a profile the caller
        # may not identify, and asking that per row re-derived the caller's own
        # friend/trip/pin sets for every relationship listed.
        others = [friendship.to_profile if friendship.from_profile_id == profile.pk else friendship.from_profile for friendship in page.friendships]
        visible_pks = Profile.visible_profile_pks(profile, others)

        return Response(
            {
                "results": [_serialize_friendship(profile, friendship, visible_pks=visible_pks) for friendship in page.friendships],
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
        if target is None or target.pk == actor.pk or not target.community_enabled or not actor.community_enabled or not Profile.visibility_permits(target.friend_request_visibility, target, actor):
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
            return Response({"error": exc.safe_message}, status=404)
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
        """Apply this view's ``services.social.friendship`` transition.

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
            return Response({"error": exc.safe_message}, status=404)
        except FriendLimitExceededError as exc:
            return Response({"error": exc.safe_message}, status=403)
        except FriendshipActionError as exc:
            return Response({"error": exc.safe_message}, status=400)

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
    """PATCH the mute state of an existing relationship; POST is a deprecated alias.

    ``PATCH {"is_muted": true|false}`` is the real endpoint: it names the state
    it wants rather than flipping whatever is there. That matters on a mobile
    link, where a request can succeed server-side while its response is lost -
    the client retries, and a toggle would silently undo the change the first
    attempt already made. With an explicit target the retry is a no-op.

    ``POST`` with no body is retained as a deprecated alias for
    ``{"is_muted": true}``, because it is what shipped first and one integration
    already calls it. It cannot unmute - a bodyless ``POST`` has no target state
    to name, so use ``PATCH`` with ``{"is_muted": false}`` for that.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SOCIAL_WRITE}),
        "PATCH": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Mute the relationship with the target - the deprecated POST path."""
        return mute_profile(actor, target)

    @extend_schema(request=FriendMuteSerializer, responses={200: FriendshipSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, profile_uuid: UUID) -> Response:
        """Set the relationship's mute state to exactly what the body asks for.

        Args:
            request: The authenticated request, carrying ``{"is_muted": bool}``.
            profile_uuid: The other profile's public uuid.

        Returns:
            The updated relationship, or an error body. 404 covers both an
            unknown uuid and a pair with no relationship row - muting is only
            offered on someone you already have a relationship with, and the
            two cases must stay indistinguishable.
        """
        target = self._resolve_target(profile_uuid)
        if target is None:
            return Response({"error": "No such profile."}, status=404)

        serializer = FriendMuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        actor = request.user.profile
        action = mute_profile if serializer.validated_data["is_muted"] else unmute_profile
        try:
            friendship = action(actor, target)
        except FriendshipNotFoundError as exc:
            return Response({"error": exc.safe_message}, status=404)
        except FriendshipActionError as exc:
            return Response({"error": exc.safe_message}, status=400)

        return Response(_serialize_friendship(actor, friendship))


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
            return Response({"error": exc.safe_message}, status=400)
        except InviteRateLimitedError as exc:
            return Response({"error": exc.safe_message}, status=429)

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
            # Never queried for a self-view: nicknaming yourself is refused at
            # write time, so the row can never exist and the lookup would be
            # pure waste on the endpoint's most common call shape.
            "nickname": None if is_self else get_annotations(viewer, target).nickname,
            "contact": None,
            "visibility": None,
            **{field: getattr(target, field) for field, _label in Profile.PREFERENCE_FIELDS},
            **{f"{field}_other": getattr(target, f"{field}_other") for field, _label in Profile.PREFERENCE_FIELDS},
            "additional_preferences": target.additional_preferences,
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

        return Response(ProfileDetailSerializer(self._redact_unreadable(request, payload)).data)

    def _redact_unreadable(self, request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Drop the read-scoped sections when the caller only holds write scope.

        ``PATCH`` answers with this same payload, and its declared scope is
        ``social:write`` alone - so returning the built profile unconditionally
        turned a write-only credential into a reader of the two things this
        endpoint's ``GET`` scopes exist to protect: the account's contact
        methods and its private visibility configuration. The escalation did
        not even require a real edit; any accepted PATCH body reached the same
        response.

        Gating the *payload* rather than only the empty-body case is what makes
        that airtight - a rule enforced on "did this request change anything"
        is one trivially-satisfied field away from being no rule at all.

        Args:
            request: The current request, carrying the credential (if any).
            payload: The fully built profile payload.

        Returns:
            The payload, with ``contact`` and ``visibility`` blanked when a
            credential caller lacks the ``GET`` scopes. Session callers hold no
            credential and are unaffected.
        """
        if request.auth is None or credential_grants(request.auth, self.required_scopes_by_method["GET"]):
            return payload
        return {**payload, "contact": None, "visibility": None}

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
            return Response({"error": exc.safe_message}, status=400)

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


# -- Trips ---------------------------------------------------------------------
#
# Every endpoint below delegates to the shared trip services
# (``services.trips.trip_access``/``trip_crud``/``trip_membership``/``trip_activities``/
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
                answer 404 - see ``services.trips.trip_access.get_trip_for_viewer``.
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
        The keyword shape ``services.trips.trip_activities.resolve_activity_place`` expects.
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
        profile = request.user.profile
        # Resolved before validation so the serializer can compare a submitted
        # date against the stored one - a PATCH sending only `end_date` has no
        # `start_date` in its payload to check it against.
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)

        serializer = TripUpdateSerializer(data=request.data, context={"instance": trip})
        serializer.is_valid(raise_exception=True)
        try:
            trip = update_trip(trip, profile, changes=serializer.validated_data)
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

    Returns ``services.trips.trip_map.build_trip_map_points`` verbatim so it stays
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
                {**TripCalendarSyncStatusSerializer(status).data, "error": "Export this trip to your calendar first with POST /trips/{trip_slug}/calendar/."},
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
        profile = request.user.profile
        # Resolved before validation so the serializer can compare a submitted
        # schedule endpoint against the stored one - moving `scheduled_at` past
        # the activity's existing `scheduled_end` is only detectable with the
        # activity in hand.
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)
        activity = TripActivity.objects.filter(pk=activity_id, trip=trip).first()

        serializer = TripActivityUpdateSerializer(data=request.data, context={"instance": activity})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        changes: dict[str, object] = {key: data[key] for key in ("title", "notes", "scheduled_at", "scheduled_end", "status", "location_hidden", "child_trip_uuid") if key in data}
        if _has_place_fields(data):
            changes["place"] = _activity_place_fields(data)

        try:
            # Left to the service to raise for a missing activity, so the
            # not-found answer stays in one place rather than being duplicated
            # from the lookup above (which exists only for validation context).
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
    ``services.trips.trip_activities.set_activity_position``.
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
