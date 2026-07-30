"""External API endpoints for direct messages and group chats.

Every view here is credential-only (``ExternalApiView``), and every one
requires a ``messages:read``/``messages:write`` scope - which
``permissions.OAUTH2_ONLY_SCOPES`` restricts to user-consented OAuth2 tokens,
so a PAT-style ``ApiKey`` can never reach any of this even if its ``scopes``
list somehow names them. A bearer key that ends up in a CI config or a
screenshot must not be a way into someone's conversations.

Two design points worth stating up front, because both are easy to "simplify"
into a bug:

**Nothing here decrypts anything.** Encrypted messages are relayed as
``ciphertext``/``nonce``/``key_version`` exactly as stored. The server holds no
key material (see ``controllers.e2ee``), and no endpoint here should ever
acquire any.

**Nothing here constructs a share row directly.** Sends that carry a pin go
through ``services.direct_message_shares.send_message_with_share``, which
routes to the same ``create_pin_share`` the web composer uses and therefore
keeps the ``LocationExposure`` provenance chain intact. Building a ``PinShare``
or ``DirectMessageShare`` inline would appear to work while silently recording
no exposure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.pagination import ExternalApiPagination
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_messaging import (
    ConversationSerializer,
    GroupChatSerializer,
    GroupCreateSerializer,
    GroupMemberSerializer,
    GroupMembersSerializer,
    GroupMessageSendSerializer,
    GroupRenameSerializer,
    MessageSendSerializer,
    MuteStateSerializer,
    PageSerializer,
    ReactionResultSerializer,
    ReactionSerializer,
    RetentionSettingsSerializer,
    build_conversation_payload,
    build_direct_message_payload,
    build_group_message_payload,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupMessage
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.direct_message_shares import ShareTargetNotFoundError, send_message_with_share
from urbanlens.dashboard.services.direct_messages import (
    THREAD_PAGE_SIZE,
    can_direct_message,
    clear_email_debounce,
    delete_message_for_everyone,
    delete_message_for_self,
    is_conversation_muted,
    is_safe_reaction_emoji,
    resolve_attachment_ids,
    set_conversation_muted,
    thread_page,
    toggle_reaction,
)
from urbanlens.dashboard.services.group_chats import (
    GROUP_THREAD_PAGE_SIZE,
    add_group_members,
    create_group_chat,
    create_group_message,
    delete_group_message,
    group_conversations_for,
    group_thread_page,
    remove_group_member,
    rename_group_chat,
    set_group_muted,
    share_pin_in_group_message,
    toggle_group_reaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from rest_framework.request import Request
    from rest_framework.serializers import BaseSerializer

    from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

logger = logging.getLogger(__name__)

#: Literal first path segments under ``messages/`` that can never be a peer
#: slug. Django resolves urlpatterns in order and the literal routes are
#: registered first, so this is a second line of defense rather than the
#: primary one - but a peer lookup that silently matched a profile actually
#: named "settings" would be a confusing, hard-to-trace bug, so the peer
#: resolver refuses these outright instead of depending on route ordering
#: staying correct forever.
RESERVED_PEER_SLUGS = frozenset({"conversations", "settings", "groups"})

#: Upper bound a client may request for one page of a message thread.
MAX_THREAD_LIMIT = 100


def _paginate_built(
    request: Request,
    rows: list[Any],
    builder: Callable[[Any], dict[str, Any]],
    serializer_class: type[BaseSerializer],
    view: Any,
) -> Response:
    """Page a list of raw rows, then build payloads for *only* that page.

    ``PaginatedListMixin`` serializes the page directly, which assumes the row
    shape already matches the serializer. These lists don't: conversation rows
    need normalizing (and that normalization issues per-row identity-masking
    queries), so paging first and building second keeps the cost proportional
    to the page rather than to the whole inbox.

    Args:
        request: The request whose ``page``/``page_size`` drive pagination.
        rows: The full, ordered row list.
        builder: Converts one raw row into a serializer-shaped dict.
        serializer_class: Serializer applied to the built page.
        view: The view, for the paginator's context.

    Returns:
        A ``{count, next, previous, results}`` response.
    """
    paginator = ExternalApiPagination()
    page = paginator.paginate_queryset(rows, request, view=view)
    built = [builder(row) for row in (page or [])]
    return paginator.get_paginated_response(serializer_class(built, many=True).data)


def _resolve_peer(peer_slug: str) -> Profile | None:
    """Resolve a conversation partner's slug to a profile.

    Args:
        peer_slug: The slug from the URL.

    Returns:
        The matching profile, or None when the slug is reserved or unknown.
    """
    if peer_slug in RESERVED_PEER_SLUGS:
        return None
    return Profile.objects.select_related("user").filter(slug=peer_slug).first()


def _thread_visible(profile: Profile, partner: Profile) -> bool:
    """Whether ``profile`` may see a conversation thread with ``partner`` at all.

    The external mirror of the check in
    ``controllers.direct_messages.ConversationView``, and it must stay identical
    to it: resolving a peer by slug is not the same as being allowed to know
    that peer exists. A profile whose DM settings reject this caller and who has
    never exchanged a message with them is hidden, so asking for the thread must
    read as "no such conversation" rather than returning an empty page - an
    empty 200 against a 404 for an invented slug is a working existence oracle
    for precisely the accounts that opted out of being reachable.

    Prior history wins over current settings deliberately: someone who has
    already talked to you does not vanish from your inbox when they later
    tighten their DM privacy.

    Args:
        profile: The requesting profile.
        partner: The resolved peer.

    Returns:
        True when a thread may be served for this pair.
    """
    return DirectMessage.objects.between(profile, partner).exists() or can_direct_message(profile, partner)


def _resolve_membership(request: Request, group_uuid: UUID) -> tuple[Profile, GroupChat, GroupChatMembership] | None:
    """Resolve the caller, the group, and the caller's active membership in it.

    Collapses "no such group", "left the group" and "never was in the group"
    into one indistinguishable None, which every caller answers with a 404.
    Group uuids are unguessable, but a distinguishable answer would still turn
    a leaked uuid into a membership oracle - and a *removed* member must not be
    able to confirm the group still exists either.

    Args:
        request: The authenticated request.
        group_uuid: The group's uuid from the URL.

    Returns:
        ``(profile, group, membership)``, or None when the caller has no active
        membership in a group with that uuid.
    """
    profile = request.user.profile
    group = GroupChat.objects.filter(uuid=group_uuid).first()
    if group is None:
        return None
    membership = group.membership_for(profile)
    if membership is None:
        return None
    return profile, group, membership


def _thread_response(request: Request, messages: list[Any], has_more_older: bool, builder: Callable[[Any], dict[str, Any]]) -> Response:
    """Build the cursor-paginated envelope for one page of a message thread.

    Deliberately *not* page-number pagination, unlike the browse lists in this
    package. A thread appends at one end continuously: with numbered pages,
    every message that arrives while a user scrolls back shifts the window, so
    page 2 re-serves rows the client already rendered from page 1 and can also
    skip rows entirely. Keying off ``before=<id>`` - the same ``before_id``
    cursor ``thread_page`` already takes - makes each request name an absolute
    position in the thread that new arrivals cannot move.

    Args:
        request: The current request, used to build the ``next`` URL.
        messages: The page's messages, oldest first.
        has_more_older: Whether older messages remain beyond this page.
        builder: Renders one message for the viewer.

    Returns:
        ``{results, next, previous, count}`` with ``previous``/``count`` null -
        a cursor walk has no page count and only goes one direction.
    """
    results = [builder(message) for message in messages]
    next_url = None
    if has_more_older and messages:
        # Oldest row in this page: the next page is everything strictly older.
        next_url = request.build_absolute_uri(f"?before={messages[0].pk}&limit={len(messages)}")
    return Response({"results": results, "next": next_url, "previous": None, "count": None})


def _thread_limit(request: Request, default: int) -> int:
    """Read and clamp the caller's requested page size.

    Args:
        request: The request carrying an optional ``limit`` query param.
        default: The page size to use when none was requested.

    Returns:
        A limit between 1 and :data:`MAX_THREAD_LIMIT`.
    """
    try:
        limit = int(request.query_params.get("limit") or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, MAX_THREAD_LIMIT))


def _before_id(request: Request) -> int | None:
    """Read the ``before`` cursor from the query string.

    Args:
        request: The request.

    Returns:
        The message id to page back from, or None for the most recent page.
    """
    try:
        return int(request.query_params["before"])
    except (KeyError, TypeError, ValueError):
        return None


class ConversationsView(ExternalApiView):
    """GET: the caller's unified inbox - one-to-one threads and group chats merged.

    Backed by ``all_conversations_for``, the same function the web sidebar
    uses, so ordering and unread counts can't drift between surfaces.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
    }

    @extend_schema(responses={200: PageSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's conversations, most recent first."""
        from urbanlens.dashboard.services.direct_messages import all_conversations_for

        profile = request.user.profile
        rows = all_conversations_for(profile)
        return _paginate_built(request, rows, lambda row: build_conversation_payload(row, profile), ConversationSerializer, self)


class MessageThreadView(ExternalApiView):
    """GET one page of a one-to-one thread; POST a new message into it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description=(
            "Returns one page of the conversation, oldest first, using cursor pagination. Pass `?before=<id>` (the `next` link does this for you) to walk further back. `previous` and `count` are always null: a live thread has no stable page count."
        ),
        responses={200: PageSerializer, 404: ErrorSerializer},
    )
    def get(self, request: Request, peer_slug: str) -> Response:
        """Return one page of the caller's conversation with ``peer_slug``."""
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        # The same gate controllers.direct_messages.ConversationView applies:
        # a thread exists for this caller only if they have history with the
        # partner or are currently permitted to message them. Without it a
        # profile that rejects the caller's messages answered 200-with-nothing
        # while an invented slug answered 404, which made this endpoint a
        # profile-existence oracle for exactly the accounts whose DM settings
        # were meant to hide them.
        if partner is None or not _thread_visible(profile, partner):
            return Response({"error": "No such conversation."}, status=404)

        messages, has_more_older = thread_page(profile, partner, before_id=_before_id(request), limit=_thread_limit(request, THREAD_PAGE_SIZE))
        return _thread_response(request, messages, has_more_older, lambda message: build_direct_message_payload(message, profile))

    @extend_schema(
        request=MessageSendSerializer,
        description=(
            "Sends a message, optionally carrying one `@pin`/`@trip`/`@friend` share. Pin shares are "
            "resolved and recorded through the same pipeline the web composer uses, so the location's "
            "share-provenance chain stays intact. Supply `client_uuid` to make retries idempotent: a repeat "
            "returns the already-created message with HTTP 200 instead of sending it twice."
        ),
        responses={201: None, 200: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, peer_slug: str) -> Response:
        """Send one message to ``peer_slug``, optionally carrying a share."""
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        if partner is None:
            return Response({"error": "No such conversation."}, status=404)

        serializer = MessageSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client_uuid = data.get("client_uuid")

        # Distinguishes "created" from "idempotent replay" for the status code,
        # without needing the service to report which happened.
        existed = bool(client_uuid) and DirectMessage.objects.filter(sender=profile, client_uuid=client_uuid).exists()

        markup_map_id = data.get("markup_map_id")
        try:
            message = send_message_with_share(
                profile,
                partner,
                data.get("body") or "",
                shared_pin_slug=data.get("shared_pin_id") or None,
                shared_trip_slug=data.get("shared_trip_slug") or None,
                shared_profile_slug=data.get("shared_profile_slug") or None,
                markup_map_uuid=str(markup_map_id) if markup_map_id else None,
                ciphertext=data.get("ciphertext") or "",
                nonce=data.get("nonce") or "",
                key_version=data.get("key_version") or 0,
                reply_to_id=data.get("reply_to_id"),
                image_ids=resolve_attachment_ids(profile, image_ids=data.get("image_ids"), image_uuids=data.get("image_uuids")),
                client_uuid=client_uuid,
            )
        except ShareTargetNotFoundError as exc:
            return Response({"error": str(exc)}, status=404)  # lgtm[py/stack-trace-exposure]
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]

        return Response(build_direct_message_payload(message, profile), status=200 if existed else 201)


class MessageThreadReadView(ExternalApiView):
    """POST: mark every message the caller has received in this thread as read."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(request=None, responses={200: None, 404: ErrorSerializer})
    def post(self, request: Request, peer_slug: str) -> Response:
        """Mark the conversation with ``peer_slug`` read."""
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        if partner is None:
            return Response({"error": "No such conversation."}, status=404)

        updated = DirectMessage.objects.between(profile, partner).filter(recipient=profile).mark_read()
        # Ends the current unread streak so a later message can alert again -
        # without this, reading on mobile would leave the streak "already
        # emailed" and suppress the next notification.
        clear_email_debounce(partner.pk, profile.pk)
        return Response({"marked_read": updated})


class MessageReactionView(ExternalApiView):
    """POST: toggle one of the caller's emoji reactions on a message."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(request=ReactionSerializer, responses={200: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, peer_slug: str, message_id: int) -> Response:
        """Add or remove the caller's reaction on one message in this thread."""
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        if partner is None:
            return Response({"error": "No such conversation."}, status=404)

        serializer = ReactionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        emoji = serializer.validated_data["emoji"]
        # Reactions are relayed verbatim into the other participant's client,
        # so the same render-safety rule the web path applies holds here.
        if not is_safe_reaction_emoji(emoji):
            return Response({"error": "That isn't a usable reaction."}, status=400)

        message = DirectMessage.objects.between(profile, partner).filter(pk=message_id).first()
        if message is None:
            return Response({"error": "No such message."}, status=404)

        try:
            action = toggle_reaction(profile, message, emoji)
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        return Response({"action": action, "reactions": build_direct_message_payload(message, profile)["reactions"]})


class MessageDetailView(ExternalApiView):
    """DELETE one message, either for everyone (sender) or just for the caller."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description=(
            "Deletes a message. `?scope=everyone` (sender only) tombstones it for the recipient and revokes "
            "any share it carried; `?scope=self` (recipient only) hides it from the caller's own view and "
            "leaves the sender's copy untouched. Defaults to `self`."
        ),
        responses={204: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def delete(self, request: Request, peer_slug: str, message_id: int) -> Response:
        """Delete one message in this thread."""
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        if partner is None:
            return Response({"error": "No such conversation."}, status=404)

        message = DirectMessage.objects.between(profile, partner).filter(pk=message_id).first()
        if message is None:
            return Response({"error": "No such message."}, status=404)

        scope = (request.query_params.get("scope") or "self").strip().lower()
        if scope not in ("everyone", "self"):
            return Response({"error": "scope must be 'everyone' or 'self'."}, status=400)

        try:
            if scope == "everyone":
                delete_message_for_everyone(message, profile)
            else:
                delete_message_for_self(message, profile)
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        return Response(status=204)


class MessageSettingsView(ExternalApiView):
    """GET/PATCH the caller's message-retention preference."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "PATCH": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(responses={200: RetentionSettingsSerializer})
    def get(self, request: Request) -> Response:
        """Return how long the caller's sent messages survive after being read."""
        profile = request.user.profile
        return Response({"direct_message_delete_after": profile.direct_message_delete_after})

    @extend_schema(
        request=RetentionSettingsSerializer,
        description="Changes retention for messages sent *from now on*; each message snapshots the setting at send time, so existing messages keep the window they were sent under.",
        responses={200: RetentionSettingsSerializer},
    )
    def patch(self, request: Request) -> Response:
        """Update the caller's retention preference."""
        serializer = RetentionSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        profile.direct_message_delete_after = serializer.validated_data["direct_message_delete_after"]
        profile.save(update_fields=["direct_message_delete_after", "updated"])
        return Response({"direct_message_delete_after": profile.direct_message_delete_after})


def _group_payload(group: GroupChat, *, member_count: int, is_muted: bool) -> dict[str, Any]:
    """Build the ``GroupChatSerializer`` shape for one group.

    Args:
        group: The group chat.
        member_count: Active member count.
        is_muted: Whether the caller muted this group.

    Returns:
        A serializer-shaped dict.
    """
    creator = group.creator
    return {
        "uuid": str(group.uuid),
        "name": group.name,
        "creator_slug": (creator.slug or None) if creator is not None else None,
        "member_count": member_count,
        "is_muted": is_muted,
        "created": group.created,
    }


class GroupsView(ExternalApiView):
    """GET the caller's group chats; POST to start a new one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(responses={200: PageSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's group chats, most recently active first."""
        profile = request.user.profile
        rows = group_conversations_for(profile)
        return _paginate_built(
            request,
            rows,
            lambda row: _group_payload(row["group"], member_count=row["member_count"], is_muted=row["is_muted"]),
            GroupChatSerializer,
            self,
        )

    @extend_schema(request=GroupCreateSerializer, responses={201: GroupChatSerializer, 400: ErrorSerializer, 403: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Create a group chat with the caller as its creator."""
        serializer = GroupCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile

        slugs = serializer.validated_data["member_slugs"]
        members = list(Profile.objects.select_related("user").filter(slug__in=slugs))
        missing = set(slugs) - {member.slug for member in members}
        if missing:
            return Response({"error": f"Unknown profile slug(s): {', '.join(sorted(missing))}."}, status=400)

        try:
            group = create_group_chat(profile, serializer.validated_data["name"], members)
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]

        return Response(_group_payload(group, member_count=group.active_memberships().count(), is_muted=False), status=201)


class GroupDetailView(ExternalApiView):
    """GET one page of a group thread; PATCH to rename the group."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "PATCH": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description="Returns one page of the group thread, cursor-paginated on `?before=<id>` exactly like a one-to-one thread.",
        responses={200: PageSerializer, 404: ErrorSerializer},
    )
    def get(self, request: Request, group_uuid: UUID) -> Response:
        """Return one page of this group's messages, oldest first."""
        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        # A non-member gets the same answer as a nonexistent group, so this
        # can't be used to probe which group uuids exist.
        membership = group.membership_for(profile) if group is not None else None
        if membership is None:
            return Response({"error": "No such group."}, status=404)

        messages, has_more_older = group_thread_page(membership, before_id=_before_id(request), limit=_thread_limit(request, GROUP_THREAD_PAGE_SIZE))
        return _thread_response(request, messages, has_more_older, lambda message: build_group_message_payload(message, profile))

    @extend_schema(request=GroupRenameSerializer, responses={200: GroupChatSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, group_uuid: UUID) -> Response:
        """Rename this group - any active member may do so."""
        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None or group.membership_for(profile) is None:
            return Response({"error": "No such group."}, status=404)

        serializer = GroupRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            group = rename_group_chat(group, profile, serializer.validated_data["name"])
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]

        membership = group.membership_for(profile)
        return Response(_group_payload(group, member_count=group.active_memberships().count(), is_muted=bool(membership and membership.muted)))


class GroupMessagesView(ExternalApiView):
    """POST a message into a group chat."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        request=GroupMessageSendSerializer,
        description=(
            "Sends a message to the group. Supply `client_uuid` for idempotent retries; a repeat returns the "
            "existing message with HTTP 200. Attachments, replies, markup maps and shares are not supported "
            "on group messages and are refused with 400 rather than silently dropped - use the group pin-share "
            "endpoint for pins."
        ),
        responses={201: None, 200: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, group_uuid: UUID) -> Response:
        """Send one message into this group."""
        from urbanlens.dashboard.models.group_chats.model import GroupMessage

        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None or group.membership_for(profile) is None:
            return Response({"error": "No such group."}, status=404)

        serializer = GroupMessageSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client_uuid = data.get("client_uuid")
        existed = bool(client_uuid) and GroupMessage.objects.filter(sender=profile, client_uuid=client_uuid).exists()

        try:
            message = create_group_message(
                profile,
                group,
                data.get("body") or "",
                ciphertext=data.get("ciphertext") or "",
                nonce=data.get("nonce") or "",
                key_version=data.get("key_version") or 0,
                client_uuid=client_uuid,
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]

        return Response(build_group_message_payload(message, profile), status=200 if existed else 201)


class GroupReadView(ExternalApiView):
    """POST: advance the caller's read mark in this group to now."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(request=None, responses={200: None, 404: ErrorSerializer})
    def post(self, request: Request, group_uuid: UUID) -> Response:
        """Mark this group's thread read for the caller."""
        from urbanlens.dashboard.models.group_chats.model import GroupMessage

        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        membership = group.membership_for(profile) if group is not None else None
        if membership is None:
            return Response({"error": "No such group."}, status=404)

        GroupMessage.objects.mark_read(membership)
        return Response({"ok": True})


class GroupMembersView(ExternalApiView):
    """GET this group's members; POST to add; DELETE to remove (or leave)."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
        "DELETE": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    def _membership_or_none(self, request: Request, group_uuid: UUID) -> tuple[Profile, GroupChat] | None:
        """Resolve the caller and group when the caller is an active member.

        Args:
            request: The authenticated request.
            group_uuid: The group's uuid.

        Returns:
            ``(profile, group)``, or None when the group is unknown or the
            caller isn't an active member.
        """
        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None or group.membership_for(profile) is None:
            return None
        return profile, group

    @extend_schema(responses={200: GroupMemberSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, group_uuid: UUID) -> Response:
        """List this group's active members, masked per the caller's visibility."""
        from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity

        resolved = self._membership_or_none(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        profile, group = resolved

        members = []
        for membership in group.active_memberships().select_related("profile", "profile__user"):
            member = membership.profile
            if member.pk == profile.pk:
                identity = {"display_name": member.username, "is_masked": False}
            else:
                identity = resolve_visible_identity(profile, member)
            members.append(
                {
                    # Blanked when masked: handing back the real slug would let
                    # the caller look up a member whose name is masked exactly
                    # to prevent that.
                    "slug": "" if identity["is_masked"] else (member.slug or ""),
                    "display_name": identity["display_name"],
                    "is_anonymized": identity["is_masked"],
                    "is_creator": group.creator_id == member.pk,
                },
            )
        return Response(GroupMemberSerializer(members, many=True).data)

    @extend_schema(request=GroupMembersSerializer, responses={200: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, group_uuid: UUID) -> Response:
        """Add members - only the group's creator may do this."""
        resolved = self._membership_or_none(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        profile, group = resolved

        serializer = GroupMembersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        slugs = serializer.validated_data["member_slugs"]

        # Permission before resolution. Resolving first made this endpoint a
        # profile-slug oracle for any active member: an unknown slug came back
        # 400 naming it, a real one got far enough to be refused 403 by
        # add_group_members, and the difference between those two answers is a
        # yes/no existence check anyone in the group could run at will. A
        # non-manager must not be able to tell the two apart, so they are
        # refused before a single submitted slug is looked at.
        if not group.is_manager(profile):
            return Response({"error": "Only the group's creator can add members."}, status=403)

        members = list(Profile.objects.select_related("user").filter(slug__in=slugs))
        missing = set(slugs) - {member.slug for member in members}
        if missing:
            return Response({"error": f"Unknown profile slug(s): {', '.join(sorted(missing))}."}, status=400)

        try:
            created = add_group_members(group, profile, members)
        except PermissionError as exc:
            # Still authoritative - the pre-check above is an anti-enumeration
            # measure, not a replacement for the service's own permission rule.
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]
        return Response({"added": len(created)})

    @extend_schema(
        request=GroupMembersSerializer,
        description="Removes members. Anyone may remove themselves (leaving the group); only the creator may remove anyone else.",
        responses={200: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def delete(self, request: Request, group_uuid: UUID) -> Response:
        """Remove members from this group, or leave it."""
        resolved = self._membership_or_none(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        profile, group = resolved

        serializer = GroupMembersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        slugs = serializer.validated_data["member_slugs"]
        targets = list(Profile.objects.select_related("user").filter(slug__in=slugs))
        missing = set(slugs) - {member.slug for member in targets}
        if missing:
            return Response({"error": f"Unknown profile slug(s): {', '.join(sorted(missing))}."}, status=400)

        # Validate the whole batch before removing anybody. Removing as we go
        # meant a batch that failed on its second target had already removed
        # the first - and notified them, and pushed the membership change to
        # every connected client - while answering 400/403 to a caller who now
        # reasonably believes nothing happened. Those side effects are not
        # transactional (a rollback cannot unsend a notification or a WebSocket
        # frame), so the fix has to be to refuse before the first mutation
        # rather than to wrap the loop.
        #
        # Both rules are re-checked by remove_group_member itself; this only
        # moves the *decision* ahead of the first side effect.
        for target in targets:
            if group.membership_for(target) is None:
                return Response({"error": "They aren't a member of this group."}, status=400)
            if target.pk != profile.pk and not group.is_manager(profile):
                return Response({"error": "Only the group's creator can remove other members."}, status=403)

        removed = 0
        for target in targets:
            try:
                remove_group_member(group, profile, target)
            except PermissionError as exc:
                return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
            except ValueError as exc:
                return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]
            removed += 1
        return Response({"removed": removed})


class GroupPinShareView(ExternalApiView):
    """POST: share one of the caller's pins into a group chat.

    Fans out one ``PinShare`` (and therefore one ``LocationExposure``) per
    connected member through ``share_pin_in_group_message`` - the same path the
    web group composer uses. Without this endpoint the mobile group composer
    would silently be unable to share pins at all.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        request=MessageSendSerializer,
        description=(
            "Shares the pin named by `shared_pin_id` (a pin slug or uuid, owned by the caller) into the "
            "group, with `body` as the accompanying text. One PinShare is created per member the caller is "
            "connected to; members they aren't connected to see the card without an accept action. Supply "
            "`client_uuid` to make retries idempotent - a repeat returns the existing message with HTTP 200 "
            "rather than re-sharing the pin to every member again."
        ),
        responses={201: None, 200: None, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, group_uuid: UUID) -> Response:
        """Share a pin into this group as a message."""
        from urbanlens.dashboard.models.pin.model import Pin

        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None or group.membership_for(profile) is None:
            return Response({"error": "No such group."}, status=404)

        serializer = MessageSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        pin_slug = data.get("shared_pin_id")
        if not pin_slug:
            return Response({"error": "shared_pin_id is required."}, status=400)

        pin = Pin.objects.slug_or_uuid(pin_slug).filter(profile=profile).first()
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        client_uuid = data.get("client_uuid")
        # Distinguishes "created" from "idempotent replay" for the status code,
        # matching the one-to-one send endpoint.
        existed = bool(client_uuid) and GroupMessage.objects.filter(sender=profile, group=group, client_uuid=client_uuid).exists()

        try:
            message = share_pin_in_group_message(profile, group, pin, data.get("body") or "", client_uuid=client_uuid)
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]

        return Response(build_group_message_payload(message, profile), status=200 if existed else 201)


class GroupMessageReactionView(ExternalApiView):
    """POST: toggle one of the caller's emoji reactions on a group message."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        request=ReactionSerializer,
        description=(
            "Adds the caller's reaction if absent, removes it if present, and returns the message's resulting "
            "reaction summary. The same emoji vocabulary rule as one-to-one reactions applies: the glyph is "
            "relayed verbatim into other members' clients, so anything that isn't render-safe is refused."
        ),
        responses={200: ReactionResultSerializer, 400: ErrorSerializer, 404: ErrorSerializer},
    )
    def post(self, request: Request, group_uuid: UUID, message_id: int) -> Response:
        """Add or remove the caller's reaction on one message in this group.

        Args:
            request: The authenticated request carrying ``emoji``.
            group_uuid: The group the message belongs to.
            message_id: The message being reacted to.

        Returns:
            200 with ``{"action", "reactions"}``; 400 for an unusable emoji;
            404 when the group, the caller's membership, or the message id
            doesn't resolve.
        """
        resolved = _resolve_membership(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        profile, group, _membership = resolved

        serializer = ReactionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        emoji = serializer.validated_data["emoji"]
        # Checked before the message is looked up, so an unusable emoji answers
        # 400 for every id - otherwise the pair of statuses (400 for a real id,
        # 404 for a fake one) would turn a junk emoji into a probe for which
        # message ids exist in this group.
        if not is_safe_reaction_emoji(emoji):
            return Response({"error": "That isn't a usable reaction."}, status=400)

        # group= in the lookup, not a follow-up check: message ids are
        # sequential across every group in the table, so pk-only would let a
        # member of any one group react into every other group's messages.
        message = GroupMessage.objects.filter(group=group, pk=message_id).first()
        if message is None:
            return Response({"error": "No such message."}, status=404)

        action = toggle_group_reaction(profile, message, emoji)
        return Response({"action": action, "reactions": build_group_message_payload(message, profile)["reactions"]})


class GroupMessageDetailView(ExternalApiView):
    """DELETE one group message, for everyone. Only the sender may."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        description=(
            "Deletes a group message for every member and revokes any pin shares it carried. There is no "
            "`?scope=self` counterpart: a group message has no per-member copy to hide, and inventing one here "
            "would diverge from the one-to-one delete semantics clients already implement. Idempotent - "
            "deleting an already-deleted message answers 204."
        ),
        responses={204: None, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def delete(self, request: Request, group_uuid: UUID, message_id: int) -> Response:
        """Delete one message in this group for everyone.

        Args:
            request: The authenticated request.
            group_uuid: The group the message belongs to.
            message_id: The message being deleted.

        Returns:
            204 on success and on a repeat; 403 when the caller isn't the
            sender; 404 when the group, the caller's membership, or the message
            id doesn't resolve.
        """
        resolved = _resolve_membership(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        profile, group, _membership = resolved

        # Scoped to the group for the same reason the reaction lookup is:
        # message ids are sequential across the whole table.
        message = GroupMessage.objects.filter(group=group, pk=message_id).first()
        if message is None:
            return Response({"error": "No such message."}, status=404)

        try:
            delete_group_message(message, profile)
        except PermissionError as exc:
            # A deliberate 403 where the rest of this package answers 404. The
            # 404-everywhere rule exists to stop a caller learning whether a
            # row exists; here they were provably already shown it - the lookup
            # above proved they are an active member of the group it is in, and
            # the thread endpoint serves them its full content - so the status
            # leaks nothing they don't have. Answering 404 instead would tell a
            # client "that message is gone" for a message still sitting in
            # their thread, which reads as a sync bug.
            return Response({"error": str(exc)}, status=403)  # lgtm[py/stack-trace-exposure]
        return Response(status=204)


class GroupLeaveView(ExternalApiView):
    """POST: leave a group chat."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(
        request=None,
        description=(
            "Ends the caller's membership. They stop receiving messages immediately and cannot read anything sent afterwards; rejoining later starts a fresh visibility window rather than restoring the old one. Idempotent - a repeat answers 204."
        ),
        responses={204: None, 404: ErrorSerializer},
    )
    def post(self, request: Request, group_uuid: UUID) -> Response:
        """Leave this group.

        POST rather than DELETE, matching the existing ``trips/<slug>/leave/``
        route: "leave" is an action on a membership the caller cannot address
        by URL, not the deletion of the group named in the path. A DELETE here
        would read as "delete this group", which no member can do.

        The membership gate is deliberately *ever* a member rather than
        *currently* an active member, and this is the one place in this module
        where those differ. Both properties are required at once: a caller who
        was never in the group must not be able to confirm it exists (404, like
        every other group route), while a caller retrying a leave whose first
        response was lost must get the same answer as the first attempt. Gating
        on active membership alone would answer the retry with 404 - which a
        client cannot distinguish from "that group never existed" - and gating
        on nothing would let a leaked uuid be probed for existence. An ended
        membership row proves the caller was shown the group, so answering them
        204 leaks nothing.

        Args:
            request: The authenticated request.
            group_uuid: The group to leave.

        Returns:
            204 on success and on a repeat; 404 when the group is unknown or
            the caller has never been a member.
        """
        from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

        profile = request.user.profile
        group = GroupChat.objects.filter(uuid=group_uuid).first()
        if group is None or not GroupChatMembership.objects.filter(group=group, profile=profile).exists():
            return Response({"error": "No such group."}, status=404)

        try:
            remove_group_member(group, profile, profile)
        except ValueError:
            # "They aren't a member of this group" - the stint was already over
            # (a repeat, or a concurrent removal between the check above and
            # this call). The caller's desired end state holds either way, so
            # this is a 204, not an error.
            return Response(status=204)
        return Response(status=204)


class ConversationMuteView(ExternalApiView):
    """GET/PUT/DELETE the caller's notification mute for one 1:1 conversation."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "PUT": frozenset({ApiKeyScope.MESSAGES_WRITE}),
        "DELETE": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(responses={200: MuteStateSerializer, 404: ErrorSerializer})
    def get(self, request: Request, peer_slug: str) -> Response:
        """Report whether the caller has muted this conversation.

        Args:
            request: The authenticated request.
            peer_slug: The conversation partner's slug.

        Returns:
            200 with ``{"is_muted": bool}``, or 404 for an unknown or reserved
            slug.
        """
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        if partner is None:
            return Response({"error": "No such conversation."}, status=404)
        return Response({"is_muted": is_conversation_muted(profile, partner)})

    @extend_schema(
        request=None,
        description=(
            "Mutes notifications for this conversation. PUT/DELETE rather than a toggling POST on purpose: a "
            "retried request over a flaky mobile link must land on the state the caller asked for instead of "
            "inverting the state the first, unacknowledged attempt already applied. Muting is "
            "notification-only - the thread keeps its place in the conversation list, keeps its unread count, "
            "and keeps receiving messages."
        ),
        responses={200: MuteStateSerializer, 404: ErrorSerializer},
    )
    def put(self, request: Request, peer_slug: str) -> Response:
        """Mute this conversation, idempotently.

        Args:
            request: The authenticated request.
            peer_slug: The conversation partner's slug.

        Returns:
            200 with ``{"is_muted": true}``, or 404 for an unknown or reserved
            slug.
        """
        return self._set(request, peer_slug, muted=True)

    @extend_schema(request=None, responses={200: MuteStateSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, peer_slug: str) -> Response:
        """Unmute this conversation, idempotently.

        Args:
            request: The authenticated request.
            peer_slug: The conversation partner's slug.

        Returns:
            200 with ``{"is_muted": false}``, or 404 for an unknown or reserved
            slug.
        """
        return self._set(request, peer_slug, muted=False)

    def _set(self, request: Request, peer_slug: str, *, muted: bool) -> Response:
        """Drive this conversation's mute flag to `muted`.

        Args:
            request: The authenticated request.
            peer_slug: The conversation partner's slug.
            muted: The desired end state.

        Returns:
            200 with the persisted state, or 404 for an unknown or reserved
            slug.
        """
        profile = request.user.profile
        partner = _resolve_peer(peer_slug)
        if partner is None:
            return Response({"error": "No such conversation."}, status=404)
        return Response({"is_muted": set_conversation_muted(profile, partner, muted=muted)})


class GroupMuteView(ExternalApiView):
    """GET/PUT/DELETE the caller's notification mute for one group chat."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.MESSAGES_READ}),
        "PUT": frozenset({ApiKeyScope.MESSAGES_WRITE}),
        "DELETE": frozenset({ApiKeyScope.MESSAGES_WRITE}),
    }

    @extend_schema(responses={200: MuteStateSerializer, 404: ErrorSerializer})
    def get(self, request: Request, group_uuid: UUID) -> Response:
        """Report whether the caller has muted this group.

        Args:
            request: The authenticated request.
            group_uuid: The group's uuid.

        Returns:
            200 with ``{"is_muted": bool}``, or 404 when the group is unknown
            or the caller is not an active member.
        """
        resolved = _resolve_membership(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        _profile, _group, membership = resolved
        return Response({"is_muted": membership.muted})

    @extend_schema(
        request=None,
        description=(
            "Mutes notifications for this group. PUT/DELETE rather than a toggling POST for the same reason as "
            "the one-to-one mute endpoint. Muting is notification-only - the group keeps its place in the "
            "conversation list and keeps accruing unread counts."
        ),
        responses={200: MuteStateSerializer, 404: ErrorSerializer},
    )
    def put(self, request: Request, group_uuid: UUID) -> Response:
        """Mute this group, idempotently.

        Args:
            request: The authenticated request.
            group_uuid: The group's uuid.

        Returns:
            200 with ``{"is_muted": true}``, or 404 when the group is unknown
            or the caller is not an active member.
        """
        return self._set(request, group_uuid, muted=True)

    @extend_schema(request=None, responses={200: MuteStateSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, group_uuid: UUID) -> Response:
        """Unmute this group, idempotently.

        Args:
            request: The authenticated request.
            group_uuid: The group's uuid.

        Returns:
            200 with ``{"is_muted": false}``, or 404 when the group is unknown
            or the caller is not an active member.
        """
        return self._set(request, group_uuid, muted=False)

    def _set(self, request: Request, group_uuid: UUID, *, muted: bool) -> Response:
        """Drive this membership's mute flag to `muted`.

        Args:
            request: The authenticated request.
            group_uuid: The group's uuid.
            muted: The desired end state.

        Returns:
            200 with the persisted state, or 404 when the group is unknown or
            the caller is not an active member.
        """
        resolved = _resolve_membership(request, group_uuid)
        if resolved is None:
            return Response({"error": "No such group."}, status=404)
        _profile, _group, membership = resolved
        return Response({"is_muted": set_group_muted(membership, muted=muted)})
