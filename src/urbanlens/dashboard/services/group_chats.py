"""Business logic for group chats.

Mirrors ``services.direct_messages``: validation and persistence live here,
both the WebSocket consumer (``DirectMessageConsumer``) and the no-JS HTTP
fallback call the same ``create_group_message``, and live delivery is
strictly best-effort. Live events are fanned out to each member's existing
per-profile direct-message channel group (``direct_message_group_name``), so
group chats need no new socket routes - every open tab of every member
already listens there.

Permission model (see ``models.group_chats``): any active member may rename
the group or leave; only the creator may add or remove members. Whether a
profile may be *added* is governed by the same
``services.direct_messages.can_direct_message`` privacy check used for
one-to-one messages, evaluated for the creator doing the adding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from urbanlens.dashboard.models.group_chats.model import MAX_GROUP_NAME_LENGTH, GroupChat, GroupChatMembership, GroupMessage, GroupMessageShare
from urbanlens.dashboard.services.direct_messages import can_direct_message, direct_message_group_name
from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Maximum number of members (including the creator) a group chat may have.
MAX_GROUP_MEMBERS = 50

#: Messages loaded per page of a group thread (matches the 1:1 THREAD_PAGE_SIZE).
GROUP_THREAD_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# "Thread open" tracking (shares the cache slot with 1:1 threads - a client
# has at most one conversation open at a time, so the same key holds either a
# partner id (int) or a "group:<id>" marker without collision).
# ---------------------------------------------------------------------------

_OPEN_THREAD_TTL_SECONDS = 90


def _open_thread_cache_key(profile_id: int) -> str:
    return f"dm_open_thread_{profile_id}"


def mark_group_thread_open(profile_id: int, group_id: int) -> None:
    """Record that `profile_id`'s client currently has this group thread open.

    Args:
        profile_id: PK of the profile viewing the thread.
        group_id: PK of the open group chat.
    """
    cache.set(_open_thread_cache_key(profile_id), f"group:{group_id}", timeout=_OPEN_THREAD_TTL_SECONDS)


def is_group_thread_open(profile_id: int, group_id: int) -> bool:
    """Return True if `profile_id` currently has this group thread open.

    Args:
        profile_id: PK of the profile who would be viewing the thread.
        group_id: PK of the group chat.

    Returns:
        True if that group thread is the one currently open for that profile.
    """
    return cache.get(_open_thread_cache_key(profile_id)) == f"group:{group_id}"


# ---------------------------------------------------------------------------
# Group lifecycle
# ---------------------------------------------------------------------------


def create_group_chat(creator: Profile, name: str, members: list[Profile]) -> GroupChat:
    """Create a group chat with `creator` plus `members`.

    The group always starts empty: even when it's spun up from an existing
    one-to-one conversation, none of that history carries over - which is the
    guarantee that people added to a conversation can never read messages
    from before the group existed.

    Args:
        creator: The profile creating (and thereafter managing) the group.
        name: The group's display name.
        members: The initial members besides the creator. Each must pass the
            creator's ``can_direct_message`` privacy check.

    Returns:
        The newly created GroupChat.

    Raises:
        ValueError: Blank/too-long name, no members, too many members, or
            duplicate members.
        PermissionError: When any member's privacy settings reject the creator.
    """
    name = name.strip()
    if not name:
        raise ValueError("A group name is required.")
    if len(name) > MAX_GROUP_NAME_LENGTH:
        raise ValueError(f"Group names are limited to {MAX_GROUP_NAME_LENGTH} characters.")

    unique_members = {member.pk: member for member in members if member.pk != creator.pk}
    if not unique_members:
        raise ValueError("Add at least one other person to start a group.")
    if len(unique_members) + 1 > MAX_GROUP_MEMBERS:
        raise ValueError(f"Groups are limited to {MAX_GROUP_MEMBERS} members.")
    for member in unique_members.values():
        if not can_direct_message(creator, member):
            raise PermissionError(f"{member.username} isn't accepting messages from you.")

    with transaction.atomic():
        group = GroupChat.objects.create(name=name, creator=creator)
        GroupChatMembership.objects.create(group=group, profile=creator)
        for member in unique_members.values():
            GroupChatMembership.objects.create(group=group, profile=member)

    for member in unique_members.values():
        # Resolved (and masked if needed) toward this specific recipient before
        # formatting - the message is stored as plain text, so it must be
        # masked here, not at render time (see identity_visibility.py).
        creator_name = resolve_visible_identity(member, creator)["display_name"]
        _notify_group_event(group, member, f"{creator_name} added you to the group “{group.name}”.")
    _broadcast_group_event(group, {"type": "group_updated", "group_uuid": str(group.uuid)})
    logger.info("Group chat %s created by profile %s with %d members", group.pk, creator.pk, len(unique_members) + 1)

    _suggest_connections_among_new_members([*unique_members.values(), creator])
    return group


def _suggest_connections_among_new_members(people: list[Profile]) -> None:
    """Soft-introduce every pair in ``people`` who isn't already connected.

    Used right after a group is created (or grown) - everyone in ``people``
    just ended up sharing a group with everyone else. Both sides of a pair
    must allow friend recommendations (see
    ``services.connections.recommendable_strangers``) - never presumes on
    anyone's behalf, just makes an already-opted-in connection discoverable.

    Args:
        people: Profiles who just started sharing this group.
    """
    from urbanlens.dashboard.services.connections import recommendable_strangers, suggest_mutual_connection

    seen_pairs: set[frozenset[int]] = set()
    for i, person in enumerate(people):
        for other in recommendable_strangers(person, people[i + 1 :]):
            pair = frozenset({person.pk, other.pk})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            suggest_mutual_connection(person, other)


def _suggest_connections_for_new_member(new_member: Profile, existing_members: list[Profile]) -> None:
    """Soft-introduce a newly added member to existing members they aren't friends with.

    Args:
        new_member: The profile that was just added to the group.
        existing_members: The group's other current members.
    """
    from urbanlens.dashboard.services.connections import recommendable_strangers, suggest_mutual_connection

    for other in recommendable_strangers(new_member, existing_members):
        suggest_mutual_connection(new_member, other)


def rename_group_chat(group: GroupChat, actor: Profile, name: str) -> GroupChat:
    """Rename `group` - any active member may do this.

    Args:
        group: The group to rename.
        actor: The acting profile.
        name: The new display name.

    Returns:
        The updated group.

    Raises:
        ValueError: Blank or over-long name.
        PermissionError: When `actor` isn't an active member.
    """
    name = name.strip()
    if not name:
        raise ValueError("A group name is required.")
    if len(name) > MAX_GROUP_NAME_LENGTH:
        raise ValueError(f"Group names are limited to {MAX_GROUP_NAME_LENGTH} characters.")
    if group.membership_for(actor) is None:
        raise PermissionError("You aren't a member of this group.")

    group.name = name
    group.save(update_fields=["name", "updated"])
    _broadcast_group_event(group, {"type": "group_updated", "group_uuid": str(group.uuid), "name": group.name})
    return group


def add_group_members(group: GroupChat, actor: Profile, members: list[Profile]) -> list[GroupChatMembership]:
    """Add `members` to `group` - only the creator may do this.

    Each addition starts a brand-new membership stint, so the new member sees
    nothing sent before this moment (see ``GroupMessageQuerySet.visible_window``).
    Encrypted groups additionally rotate their key on the next send, so old
    ciphertext is never even decryptable by the newcomer.

    Args:
        group: The group being extended.
        actor: The acting profile (must be the group's creator).
        members: Profiles to add. Already-active members are skipped.

    Returns:
        The newly created membership rows.

    Raises:
        ValueError: When adding would exceed ``MAX_GROUP_MEMBERS``.
        PermissionError: When `actor` isn't the creator, or a member's privacy
            settings reject them.
    """
    if not group.is_manager(actor):
        raise PermissionError("Only the group's creator can add members.")

    active_ids = set(group.active_memberships().values_list("profile_id", flat=True))
    to_add = {member.pk: member for member in members if member.pk not in active_ids}
    if not to_add:
        return []
    if len(active_ids) + len(to_add) > MAX_GROUP_MEMBERS:
        raise ValueError(f"Groups are limited to {MAX_GROUP_MEMBERS} members.")
    for member in to_add.values():
        if not can_direct_message(actor, member):
            raise PermissionError(f"{member.username} isn't accepting messages from you.")

    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    existing_members = list(ProfileModel.objects.filter(pk__in=active_ids))
    created = [GroupChatMembership.objects.create(group=group, profile=member) for member in to_add.values()]
    for member in to_add.values():
        actor_name = resolve_visible_identity(member, actor)["display_name"]
        _notify_group_event(group, member, f"{actor_name} added you to the group “{group.name}”.")
    _broadcast_group_event(group, {"type": "group_updated", "group_uuid": str(group.uuid)})
    logger.info("Profile %s added %d members to group %s", actor.pk, len(created), group.pk)

    for member in to_add.values():
        _suggest_connections_for_new_member(member, existing_members)
    return created


def remove_group_member(group: GroupChat, actor: Profile, target: Profile) -> None:
    """End `target`'s membership in `group`.

    Anyone may remove themselves (leave); only the creator may remove someone
    else. The membership row is kept (with ``left_at``/``removed_by`` set) so
    the visibility window of what they already saw stays on record; they lose
    all access to the group from here on.

    Args:
        group: The group.
        actor: The acting profile.
        target: The member being removed.

    Raises:
        PermissionError: When `actor` is neither `target` nor the creator.
        ValueError: When `target` has no active membership.
    """
    membership = group.membership_for(target)
    if membership is None:
        raise ValueError("They aren't a member of this group.")
    if actor.pk != target.pk and not group.is_manager(actor):
        raise PermissionError("Only the group's creator can remove other members.")

    membership.end(removed_by=actor if actor.pk != target.pk else None)
    if actor.pk != target.pk:
        _notify_group_event(group, target, f"You were removed from the group “{group.name}”.")
    # The removed member's client also gets the event (their sidebar entry
    # should disappear), so broadcast before recomputing the member set is
    # not required - _broadcast_group_event reads current active members, and
    # the removed member is pinged separately.
    _broadcast_group_event(group, {"type": "group_updated", "group_uuid": str(group.uuid)}, extra_profile_ids=[target.pk])
    logger.info("Profile %s ended membership of profile %s in group %s", actor.pk, target.pk, group.pk)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def serialize_group_message(message: GroupMessage, *, viewer: Profile | None = None) -> dict[str, Any]:
    """Serialize a group message into the JSON payload pushed over the WebSocket.

    Args:
        message: The message to serialize.
        viewer: The member this payload will be delivered to. When given, the
            sender's name is resolved through ``resolve_visible_identity`` so
            a live incoming message never reveals a name the server-rendered
            thread would mask for that viewer (docs/PROBLEMS.md; decision
            2026-07-23: per-recipient payloads). None keeps the raw name -
            correct only for the sender's own sessions.

    Returns:
        A JSON-serializable dict; ``group_uuid`` lets the frontend route the
        payload to the right open conversation.
    """
    if viewer is None or viewer.pk == message.sender_id:
        sender_name = message.sender.username
    else:
        sender_name = resolve_visible_identity(viewer, message.sender)["display_name"]
    return {
        "type": "group_message",
        "id": message.pk,
        "group_uuid": str(message.group.uuid),
        "group_name": message.group.name,
        "body": message.body,
        "ciphertext": message.ciphertext,
        "nonce": message.nonce,
        "key_version": message.key_version,
        "created": message.created.isoformat(),
        "sender_slug": message.sender.slug or "",
        "sender_name": sender_name,
        # Shares need the full server-rendered card - the client re-fetches
        # the thread partial when this is set (same contract as 1:1 has_share).
        "has_share": message.shares.exists(),
    }


def _broadcast_group_event(group: GroupChat, payload: dict[str, Any], *, extra_profile_ids: list[int] | None = None) -> None:
    """Push `payload` to every active member's live sessions after commit.

    Best-effort: a channel-layer failure is logged, never raised.

    Args:
        group: The group whose members should receive the event.
        payload: JSON-serializable dict to deliver.
        extra_profile_ids: Additional profile PKs to deliver to (e.g. a
            just-removed member whose sidebar must update).
    """
    profile_ids = set(group.active_memberships().values_list("profile_id", flat=True))
    profile_ids.update(extra_profile_ids or [])
    groups = {direct_message_group_name(profile_id) for profile_id in profile_ids}

    def _send() -> None:
        layer = get_channel_layer()
        if layer is None:
            return
        for channel_group in groups:
            try:
                async_to_sync(layer.group_send)(channel_group, {"type": "dm.message", "message": payload})
            except Exception:
                logger.warning("Live push of group event to %s failed", channel_group, exc_info=True)

    transaction.on_commit(_send)


def _notify_group_event(group: GroupChat, recipient: Profile, text: str) -> None:
    """Raise an on-site notification about a group membership event.

    Args:
        group: The group the event concerns.
        recipient: The profile to notify.
        text: The notification body.
    """
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    try:
        pref = recipient.notification_preferences.message
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return
    NotificationLog.objects.create(
        profile=recipient,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.MESSAGE,
        title=group.name,
        message=text,
        url=reverse("messages.group", kwargs={"group_uuid": group.uuid}),
    )


def _notify_group_message(message: GroupMessage) -> None:
    """Raise on-site notifications for a new group message.

    One notification per member per "conversation went unread" (mirrors the
    1:1 behavior): a member with other unread messages in this group isn't
    re-notified for every message in a burst. Muted memberships and members
    currently looking at the thread are skipped.

    Args:
        message: The freshly created message.
    """
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    if message.is_encrypted:
        preview = "🔒 Encrypted message"
    elif message.body:
        preview = message.body if len(message.body) <= 120 else message.body[:120].rstrip() + "…"
    else:
        preview = "New message"

    group = message.group
    url = reverse("messages.group", kwargs={"group_uuid": group.uuid})
    memberships = group.active_memberships().exclude(profile_id=message.sender_id).select_related("profile", "profile__user")
    for membership in memberships:
        if membership.muted or is_group_thread_open(membership.profile_id, group.pk):
            continue
        already_unread = GroupMessage.objects.unread_for(membership).exclude(pk=message.pk).exists()
        if already_unread:
            continue
        try:
            pref = membership.profile.notification_preferences.message
        except AttributeError:
            pref = DeliveryPreference.SITE
        # Unlike the 1:1 path (which suppresses the in-app row for EMAIL-only
        # users because a delayed email actually goes out instead), group
        # messages have no email channel - so anything short of NONE still
        # gets the in-app row rather than silently nothing.
        if pref == DeliveryPreference.NONE:
            continue
        # Same viewer-scoped masking the thread render applies - a sender whose
        # profile_visibility hides them from this member must not be revealed
        # by the bell notification before the (masked) thread is even opened.
        sender_display_name = resolve_visible_identity(membership.profile, message.sender)["display_name"]
        NotificationLog.objects.create(
            profile=membership.profile,
            source_profile=message.sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.MESSAGE,
            title=f"New message in {group.name}",
            message=f"{sender_display_name}: {preview}",
            url=url,
        )


def create_group_message(
    sender: Profile,
    group: GroupChat,
    body: str,
    *,
    ciphertext: str = "",
    nonce: str = "",
    key_version: int = 0,
    defer_broadcast: bool = False,
) -> GroupMessage:
    """Validate, persist, broadcast, and notify for one new group message.

    Args:
        sender: The sending profile (must be an active member).
        group: The group being messaged.
        body: Plaintext message text. Must be blank when ``ciphertext`` is given.
        ciphertext: End-to-end encrypted body (base64), produced client-side
            under the group key. Mutually exclusive with ``body``.
        nonce: Base64 nonce for ``ciphertext`` (required with it).
        key_version: ``GroupKey.version`` that encrypted this message.
        defer_broadcast: When True, skip the live push - the caller attaches
            shares first and then calls ``broadcast_group_message``.

    Returns:
        The newly created GroupMessage.

    Raises:
        ValueError: Empty/too-long/malformed content.
        PermissionError: When `sender` isn't an active member.
    """
    from urbanlens.dashboard.services.e2ee import MAX_CIPHERTEXT_LENGTH, MAX_NONCE_LENGTH, valid_blob

    membership = group.membership_for(sender)
    if membership is None:
        raise PermissionError("You aren't a member of this group.")

    body = body.strip()
    if len(body) > MAX_DIRECT_MESSAGE_LENGTH:
        raise ValueError(f"Message is too long (max {MAX_DIRECT_MESSAGE_LENGTH:,} characters).")
    if ciphertext:
        if body:
            raise ValueError("A message is either plaintext or encrypted, never both.")
        if not valid_blob(ciphertext, MAX_CIPHERTEXT_LENGTH) or not valid_blob(nonce, MAX_NONCE_LENGTH) or key_version < 1:
            raise ValueError("Malformed encrypted message.")
    elif nonce or key_version:
        raise ValueError("Malformed encrypted message.")
    if not body and not ciphertext:
        raise ValueError("Message cannot be empty.")

    message = GroupMessage.objects.create(
        group=group,
        sender=sender,
        body=body,
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=key_version,
    )
    # Sending is reading: the sender's own read mark advances with their message.
    GroupMessage.objects.mark_read(membership)

    _notify_group_message(message)
    if not defer_broadcast:
        broadcast_group_message(message)
    # DEBUG, not INFO: fires on every group message sent, same reasoning as
    # the 1:1 direct-message send log in services.direct_messages.
    logger.debug("Group message %s: profile %s -> group %s", message.pk, sender.pk, group.pk)
    return message


def broadcast_group_message(message: GroupMessage) -> None:
    """Push `message` to every active member's live sessions now.

    Unlike the identity-free group events (``group_updated`` etc., which go
    through ``_broadcast_group_event`` with one shared payload), a message
    payload carries the sender's name - so it is built once per member, with
    the name resolved through that member's own visibility. The per-member
    resolution cost is the accepted price of never leaking a masked name
    through the live channel (decision 2026-07-23).

    Args:
        message: The message to broadcast.
    """
    members = list(message.group.active_memberships().select_related("profile__user"))

    deliveries = [(direct_message_group_name(membership.profile_id), serialize_group_message(message, viewer=membership.profile)) for membership in members]

    def _send() -> None:
        layer = get_channel_layer()
        if layer is None:
            return
        for channel_group, payload in deliveries:
            try:
                async_to_sync(layer.group_send)(channel_group, {"type": "dm.message", "message": payload})
            except Exception:
                logger.warning("Live push of group message %s to %s failed", message.pk, channel_group, exc_info=True)

    transaction.on_commit(_send)


def delete_group_message(message: GroupMessage, actor: Profile) -> GroupMessage:
    """Delete `message` for everyone - only the sender may do this.

    Args:
        message: The message to delete.
        actor: The profile requesting the delete.

    Returns:
        The updated message.

    Raises:
        PermissionError: If `actor` isn't the message's sender.
    """
    if actor.pk != message.sender_id:
        raise PermissionError("Only the sender can delete this message.")
    if message.deleted_at is None:
        message.deleted_at = timezone.now()
        message.save(update_fields=["deleted_at", "updated"])
        for share in message.shares.select_related("pin_share"):
            if share.pin_share is not None:
                _revoke_pin_share(share.pin_share)
        _broadcast_group_event(
            message.group,
            {"type": "group_message_deleted", "group_uuid": str(message.group.uuid), "message_id": message.pk},
        )
    return message


def _revoke_pin_share(pin_share) -> None:
    """Reject a still-pending PinShare when its group message is deleted.

    Args:
        pin_share: The PinShare attached to the deleted message.
    """
    from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

    if pin_share.status == PinShareStatus.PENDING:
        pin_share.status = PinShareStatus.REJECTED
        pin_share.save(update_fields=["status"])


def share_pin_in_group_message(sender: Profile, group: GroupChat, pin: Pin, body: str) -> GroupMessage:
    """Share `pin` into `group` - one full PinShare per member, plus the chat message.

    Every active member (other than the sender) gets their own ``PinShare``
    through :func:`~urbanlens.dashboard.services.pin_sharing.create_pin_share`
    - notification, provenance stamping, and exposure recording included - so
    a group share counts exactly like sharing with each member individually.
    Members the sender isn't connected to are skipped (the friends-only rule
    is per recipient); they still see the message and card, just without an
    accept action.

    Args:
        sender: The sharing profile (must own the pin and be an active member).
        group: The group receiving the share.
        pin: The pin being shared.
        body: Message text accompanying the share (may be blank; a default
            "shared a pin" text is used so the message isn't empty).

    Returns:
        The newly created GroupMessage.

    Raises:
        PermissionError: When `sender` isn't an active member.
        ValueError: Propagated from `create_group_message` for bad input.
    """
    from urbanlens.dashboard.services.pin_sharing import create_pin_share

    message = create_group_message(sender, group, body or f"Shared {pin.display_label}", defer_broadcast=True)
    for membership in group.active_memberships().exclude(profile_id=sender.pk).select_related("profile", "profile__user"):
        try:
            pin_share = create_pin_share(sender, membership.profile, pin)
        except PermissionError:
            # Not connected to this member - the friends-only sharing rule
            # applies per recipient; they see the card without an action.
            continue
        GroupMessageShare.objects.create(message=message, recipient=membership.profile, pin_share=pin_share)
    broadcast_group_message(message)
    return message


# ---------------------------------------------------------------------------
# Reading / listing
# ---------------------------------------------------------------------------


def group_thread_page(membership: GroupChatMembership, *, before_id: int | None = None, limit: int = GROUP_THREAD_PAGE_SIZE) -> tuple[list[GroupMessage], bool]:
    """Return one page of a group thread, mirroring ``direct_messages.thread_page``.

    Args:
        membership: The viewer's active membership (scopes visibility).
        before_id: When given, only messages with a smaller pk are considered;
            None loads the most recent page.
        limit: Maximum number of messages to return.

    Returns:
        ``(messages, has_more_older)``: messages oldest-first;
        ``has_more_older`` is True when older visible messages remain.
    """
    queryset = GroupMessage.objects.visible_window(membership).select_related("sender", "sender__user").prefetch_related("shares__pin_share__pin", "shares__pin_share__pin__location")
    if before_id is not None:
        queryset = queryset.filter(pk__lt=before_id)
    page = list(queryset.order_by("-id")[: limit + 1])
    has_more_older = len(page) > limit
    page = page[:limit]
    page.reverse()
    return page, has_more_older


def group_conversations_for(profile: Profile) -> list[dict[str, Any]]:
    """Return the profile's group conversations, most recently active first.

    Args:
        profile: The profile whose group inbox to build.

    Returns:
        A list of dicts with ``kind="group"``, ``group`` (GroupChat),
        ``last_message`` (GroupMessage or None), ``last_sender_display_name``
        (the last sender's viewer-scoped masked-if-needed name, "" when no
        message), ``unread_count`` (int), ``member_count`` (int),
        ``is_muted`` (bool), and ``last_activity`` (datetime used for
        cross-kind sorting).

    Runs a fixed number of queries regardless of how many groups the profile
    is in, rather than three queries per group (last message, unread count,
    member count) - this backs the messages sidebar, which refreshes after
    nearly every send/receive, so a per-group query loop here compounds fast.
    Each membership's own visibility window (its own ``created``/
    ``last_read_at`` cutoffs) can't be expressed as one shared filter, but
    the per-group breakdowns can still come from two grouped queries instead
    of one query each.
    """
    memberships = list(GroupChatMembership.objects.active().filter(profile=profile).select_related("group"))
    if not memberships:
        return []
    group_ids = [membership.group_id for membership in memberships]

    member_counts = dict(
        GroupChatMembership.objects.active().filter(group_id__in=group_ids).values_list("group_id").annotate(count=Count("id")).order_by(),
    )

    visible = Q(pk__in=[])
    unread = Q(pk__in=[])
    for membership in memberships:
        visible |= Q(group_id=membership.group_id, created__gte=membership.created)
        unread_clause = Q(group_id=membership.group_id, created__gte=membership.created) & ~Q(sender_id=membership.profile_id)
        if membership.last_read_at is not None:
            unread_clause &= Q(created__gt=membership.last_read_at)
        unread |= unread_clause

    last_message_by_group: dict[int, GroupMessage] = {}
    for message in GroupMessage.objects.filter(visible).select_related("sender", "sender__user").order_by("group_id", "-id"):
        last_message_by_group.setdefault(message.group_id, message)
    unread_counts = dict(GroupMessage.objects.filter(unread).values_list("group_id").annotate(count=Count("id")).order_by())

    # The sidebar preview shows the last sender's name - resolve it through the
    # same viewer-scoped identity masking the thread render uses, so a sender
    # whose profile_visibility hides them from this viewer isn't revealed by
    # the preview line before the (masked) thread is even opened.
    sender_display_names: dict[int, str] = {}
    for message in last_message_by_group.values():
        if message.sender_id not in sender_display_names:
            sender_display_names[message.sender_id] = resolve_visible_identity(profile, message.sender)["display_name"]

    conversations: list[dict[str, Any]] = []
    for membership in memberships:
        group = membership.group
        last_message = last_message_by_group.get(membership.group_id)
        conversations.append(
            {
                "kind": "group",
                "group": group,
                "last_message": last_message,
                "last_sender_display_name": sender_display_names.get(last_message.sender_id, "") if last_message is not None else "",
                "unread_count": unread_counts.get(membership.group_id, 0),
                "member_count": member_counts.get(membership.group_id, 0),
                "is_muted": membership.muted,
                "last_activity": last_message.created if last_message is not None else membership.created,
            },
        )
    conversations.sort(key=lambda conv: conv["last_activity"], reverse=True)
    return conversations


def unread_group_conversation_count(profile: Profile) -> int:
    """Count the profile's groups with at least one unread message.

    Args:
        profile: The member profile.

    Returns:
        The number of groups needing attention (feeds the navbar label,
        alongside the 1:1 ``unread_conversation_count``).

    Runs exactly two queries regardless of how many groups the profile is
    in - this backs a site-wide 60-second poll (every open page, every
    logged-in user), so a per-membership query loop here is not just slow
    for one request but a real aggregate load multiplier across the whole
    site. Each membership's own visibility window (its own ``created``/
    ``last_read_at`` cutoffs - see ``GroupMessageQuerySet.unread_for``)
    can't be expressed as one shared filter, but can still be OR'd together
    into a single query, matching the same technique
    ``GroupMessageQuerySet.search_visible_to`` already uses for the analogous
    "any of my memberships" problem.
    """
    memberships = list(GroupChatMembership.objects.active().filter(profile=profile))
    if not memberships:
        return 0
    visibility = Q(pk__in=[])
    for membership in memberships:
        clause = Q(group_id=membership.group_id, created__gte=membership.created) & ~Q(sender_id=membership.profile_id)
        if membership.last_read_at is not None:
            clause &= Q(created__gt=membership.last_read_at)
        visibility |= clause
    return GroupMessage.objects.filter(visibility).values("group_id").distinct().count()


def group_e2ee_ready(group: GroupChat) -> bool:
    """Return True when every active member has published a key bundle.

    Group messages are encrypted iff *all* current members are enrolled -
    otherwise the group falls back to plaintext (matching the 1:1
    opportunistic-encryption rule).

    Args:
        group: The group to check.

    Returns:
        True when the group can encrypt.
    """
    from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

    member_ids = list(group.active_memberships().values_list("profile_id", flat=True))
    enrolled = MessagingKeyBundle.objects.for_profiles(member_ids).count()
    return enrolled == len(member_ids) and len(member_ids) > 0
