"""Serializers and response builders for the external API's messaging surface.

Split from ``serializers.py`` because messaging carries a constraint the rest
of the external API does not: **the server must never widen what it can see
about a conversation.**

Two rules follow, and they are not negotiable:

1. No field here is ever derived from decrypted plaintext. ``body`` is only
   ever the plaintext a user *chose* to send unencrypted; an encrypted message
   yields ``ciphertext``/``nonce``/``key_version`` and nothing more. There is
   deliberately no "decrypt this for me" endpoint - the server has no key
   material and adding one would mean introducing some, which would end
   end-to-end encryption outright.
2. Every payload naming a person is built through the existing
   ``serialize_direct_message`` / ``resolve_visible_identity`` path rather than
   reading ``profile.username`` directly. Those functions implement per-viewer
   identity masking (the 2026-07-23 fix): a sender whose ``profile_visibility``
   hides them from this viewer must appear as a placeholder here exactly as
   they do in the web thread. Bypassing them re-leaks masked names over the
   mobile API, where nobody would notice for a long time.

The builder functions below exist to enforce (2) structurally: views call a
builder, never a serializer, so there is no route to a response that skipped
the masking step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rest_framework import serializers

from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.group_chats.model import MAX_GROUP_NAME_LENGTH
from urbanlens.dashboard.services.direct_messages import reaction_summary, serialize_direct_message
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupMessage
    from urbanlens.dashboard.models.profile.model import Profile

#: Upper bound on a submitted nonce, matching ``services.e2ee.MAX_NONCE_LENGTH``.
MAX_NONCE_FIELD_LENGTH = 64

#: Upper bound on how many images one send may attach. Generous next to any
#: real composer, purely a guard against an unbounded list.
MAX_ATTACHED_IMAGES = 20

#: Upper bound on members named in one group create / add-members call.
MAX_MEMBER_SLUGS = 50


class MessageBodySerializer(serializers.Serializer):
    """The plaintext-or-ciphertext half of any message payload.

    Exactly one side is ever populated: a plaintext message reports
    ``ciphertext``/``nonce`` as ``""`` and ``key_version`` as ``0``; an
    encrypted one reports ``body`` as ``""``. ``is_encrypted`` saves clients
    from having to infer that from empty strings.
    """

    body = serializers.CharField(read_only=True, allow_blank=True, help_text='Plaintext body, or "" when the message is encrypted.')
    ciphertext = serializers.CharField(read_only=True, allow_blank=True, help_text='Base64 ciphertext, or "" when the message is plaintext.')
    nonce = serializers.CharField(read_only=True, allow_blank=True, help_text='Base64 nonce, or "" when the message is plaintext.')
    key_version = serializers.IntegerField(read_only=True, help_text="Conversation/group key version that encrypted this message; 0 for plaintext.")
    is_encrypted = serializers.BooleanField(read_only=True)


class DirectMessageShareSerializer(serializers.Serializer):
    """The `@pin` / `@trip` / `@friend` offer attached to a message, if any.

    Identifiers only - resolving them is a separate, separately-authorized
    request. In particular a pin share exposes the ``PinShare`` id rather than
    the pin's coordinates, so receiving a share does not by itself hand the
    recipient the location before they accept it.
    """

    kind = serializers.ChoiceField(read_only=True, choices=["pin", "trip", "friend"])
    pin_share_id = serializers.IntegerField(read_only=True, allow_null=True)
    trip_slug = serializers.CharField(read_only=True, allow_null=True)
    recommended_profile_slug = serializers.CharField(read_only=True, allow_null=True)
    revoked = serializers.BooleanField(read_only=True)


class DirectMessageSerializer(MessageBodySerializer):
    """One message as seen *by one particular viewer*.

    Viewer-dependent by construction, not incidentally: ``tombstone`` reflects
    the sender's retention setting applied to this viewer (the sender keeps
    seeing their own message forever; the recipient stops), and the sender's
    displayed identity is resolved through this viewer's visibility. The same
    row therefore serializes differently for the two participants, which is
    correct and must not be "optimized" into a shared cached payload.
    """

    id = serializers.IntegerField(read_only=True)
    sender_slug = serializers.CharField(read_only=True, allow_blank=True)
    recipient_slug = serializers.CharField(read_only=True, allow_blank=True)
    sender_name = serializers.CharField(read_only=True, allow_blank=True, help_text="Sender's display name as this viewer may see it - masked when their profile visibility hides them.")
    created = serializers.DateTimeField(read_only=True)
    read_at = serializers.DateTimeField(read_only=True, allow_null=True)
    reply_to_id = serializers.IntegerField(read_only=True, allow_null=True)
    sender_delete_after = serializers.CharField(read_only=True)
    expires_for_recipient = serializers.BooleanField(read_only=True)
    tombstone = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Placeholder text when this viewer may no longer see the content (deleted or expired). Non-null means body/ciphertext are blanked.",
    )
    reactions = serializers.ListField(read_only=True, child=serializers.DictField())
    share = DirectMessageShareSerializer(read_only=True, allow_null=True)
    pin_share_id = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text=(
            "Id of the PinShare this viewer may answer via POST /pin-shares/{share_id}/respond/, or null. Flat rather than "
            "only nested under `share` because a group message has no single share object - a pin shared into a group "
            "creates one row per recipient - so this is the one field a client can read for either conversation kind."
        ),
    )
    markup_map_uuid = serializers.CharField(read_only=True, allow_null=True)
    map_removed = serializers.BooleanField(read_only=True)
    image_ids = serializers.ListField(read_only=True, child=serializers.IntegerField())


class ConversationSerializer(serializers.Serializer):
    """One row of the unified inbox - a one-to-one thread or a group chat.

    One shape for both kinds so a client renders a single merged, recency-
    ordered list (which is what ``all_conversations_for`` returns). ``kind``
    discriminates; the fields that only make sense for one kind are null for
    the other.
    """

    kind = serializers.ChoiceField(read_only=True, choices=["dm", "group"])
    peer_slug = serializers.CharField(read_only=True, allow_null=True, help_text="The partner's slug. Null for group conversations.")
    group_uuid = serializers.CharField(read_only=True, allow_null=True, help_text="The group's uuid. Null for one-to-one conversations.")
    creator_slug = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Slug of the group's creator - the only member who may add or remove others. Null for one-to-one conversations and for a group whose creator deleted their account.",
    )
    display_name = serializers.CharField(read_only=True, help_text="Group name, or the partner's viewer-resolved (possibly masked) name.")
    display_avatar_url = serializers.CharField(read_only=True, allow_null=True)
    is_anonymized = serializers.BooleanField(read_only=True, help_text="True when the partner's identity is masked from this viewer. Always false for groups.")
    unread_count = serializers.IntegerField(read_only=True)
    member_count = serializers.IntegerField(read_only=True, allow_null=True, help_text="Active member count. Null for one-to-one conversations.")
    is_muted = serializers.BooleanField(read_only=True)
    last_activity = serializers.DateTimeField(read_only=True)
    last_message = DirectMessageSerializer(read_only=True, allow_null=True)


class GroupChatSerializer(serializers.Serializer):
    """A group chat's own identity and the caller's standing in it."""

    uuid = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    creator_slug = serializers.CharField(read_only=True, allow_null=True)
    member_count = serializers.IntegerField(read_only=True)
    is_muted = serializers.BooleanField(read_only=True)
    created = serializers.DateTimeField(read_only=True)


class GroupMemberSerializer(serializers.Serializer):
    """One active member, named as this viewer is permitted to see them."""

    slug = serializers.CharField(read_only=True, allow_blank=True, help_text="Blank when this member's identity is masked from the viewer - a masked member must not be lookup-able by slug.")
    display_name = serializers.CharField(read_only=True)
    is_anonymized = serializers.BooleanField(read_only=True)
    is_creator = serializers.BooleanField(read_only=True)


class MessageSendSerializer(serializers.Serializer):
    """Validates an untrusted "send a message" payload.

    Mirrors the invariants ``create_direct_message`` enforces server-side
    rather than replacing them - this is the first line of defense on
    untrusted input, and the service remains the authority regardless of
    caller.
    """

    body = serializers.CharField(required=False, allow_blank=True, default="", max_length=MAX_DIRECT_MESSAGE_LENGTH)
    ciphertext = serializers.CharField(required=False, allow_blank=True, default="")
    nonce = serializers.CharField(required=False, allow_blank=True, default="", max_length=MAX_NONCE_FIELD_LENGTH)
    key_version = serializers.IntegerField(required=False, default=0, min_value=0)
    reply_to_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    markup_map_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    image_ids = serializers.ListField(required=False, child=serializers.IntegerField(), max_length=MAX_ATTACHED_IMAGES, default=list)
    #: A pin *slug* despite the ``_id`` suffix, matching the naming this
    #: application already uses for pin references on the wire (the same
    #: `slug_or_uuid` lookup accepts either). Renaming it here would be more
    #: accurate and less consistent; consistency wins for a public contract.
    shared_pin_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None, max_length=255)
    shared_trip_slug = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None, max_length=255)
    shared_profile_slug = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None, max_length=255)
    #: Idempotency key for offline outbox retries - a repeat is answered with
    #: the already-created message rather than delivering it twice.
    client_uuid = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate(self, attrs: dict) -> dict:
        """Enforce the content and share invariants.

        Args:
            attrs: The deserialized payload.

        Returns:
            The validated payload.

        Raises:
            rest_framework.serializers.ValidationError: Content is both
                plaintext and encrypted, is empty with nothing attached, is
                encrypted without a usable nonce/key version, or names more
                than one share.
        """
        body = (attrs.get("body") or "").strip()
        ciphertext = attrs.get("ciphertext") or ""
        shares = [attrs.get("shared_pin_id"), attrs.get("shared_trip_slug"), attrs.get("shared_profile_slug")]
        present_shares = [share for share in shares if share]

        if len(present_shares) > 1:
            raise serializers.ValidationError("A message can carry only one of shared_pin_id, shared_trip_slug or shared_profile_slug.")

        if body and ciphertext:
            raise serializers.ValidationError("A message is either plaintext or encrypted, never both.")

        if ciphertext:
            # key_version identifies which conversation/group key opens this
            # ciphertext; version 0 means plaintext, so an encrypted message
            # claiming it could never be decrypted by anyone.
            if not (attrs.get("nonce") or "").strip():
                raise serializers.ValidationError("An encrypted message requires a nonce.")
            if (attrs.get("key_version") or 0) < 1:
                raise serializers.ValidationError("An encrypted message requires key_version >= 1.")
        elif attrs.get("key_version"):
            raise serializers.ValidationError("key_version is only meaningful for an encrypted message.")

        if not body and not ciphertext and not present_shares and not attrs.get("image_ids") and not attrs.get("markup_map_id"):
            raise serializers.ValidationError("Message cannot be empty.")

        return attrs


class GroupCreateSerializer(serializers.Serializer):
    """Validates a "start a group chat" payload."""

    name = serializers.CharField(max_length=MAX_GROUP_NAME_LENGTH)
    member_slugs = serializers.ListField(child=serializers.CharField(max_length=255), max_length=MAX_MEMBER_SLUGS, allow_empty=False)


class GroupRenameSerializer(serializers.Serializer):
    """Validates a group rename. Any active member may rename a group."""

    name = serializers.CharField(max_length=MAX_GROUP_NAME_LENGTH)


class GroupMembersSerializer(serializers.Serializer):
    """Validates an add-members / remove-member payload."""

    member_slugs = serializers.ListField(child=serializers.CharField(max_length=255), max_length=MAX_MEMBER_SLUGS, allow_empty=False)


class ReactionSerializer(serializers.Serializer):
    """Validates a reaction toggle.

    Length-capped here; ``services.direct_messages.is_safe_reaction_emoji``
    remains the authority on whether the glyph is render-safe, since reactions
    are relayed verbatim into other participants' clients.
    """

    emoji = serializers.CharField(max_length=32)


class ReactionResultSerializer(serializers.Serializer):
    """What a reaction toggle did, plus the target's resulting summary.

    ``action`` reports the transition rather than the state, matching the
    1:1 reaction endpoint it mirrors; ``reactions`` is the authoritative
    post-write summary, so a client that ignores ``action`` still renders the
    right thing.
    """

    action = serializers.ChoiceField(read_only=True, choices=["added", "removed"])
    reactions = serializers.ListField(read_only=True, child=serializers.DictField())


class MuteStateSerializer(serializers.Serializer):
    """One conversation's notification-mute state.

    The response body for the mute endpoints, which are PUT/DELETE rather than
    a toggling POST: a retried request over a flaky link must land on the state
    the caller asked for, not the opposite of it. The body therefore reports
    the persisted state rather than "ok", so a client that lost the first
    response can reconcile without a second round trip.

    Muting suppresses notifications only. The conversation stays in the
    conversation list, keeps its unread count, and keeps receiving messages.
    """

    is_muted = serializers.BooleanField(read_only=True)


class RetentionSettingsSerializer(serializers.Serializer):
    """The caller's message-retention preference."""

    direct_message_delete_after = serializers.ChoiceField(choices=MessageRetentionChoice.choices)


class PageSerializer(serializers.Serializer):
    """The list envelope shared by this surface's paginated endpoints.

    ``next``/``previous``/``count`` are nullable because the message-thread
    endpoint is cursor-paginated rather than page-numbered: it can offer a
    "further back" link but has no page count and no notion of a previous
    page. See ``views_messaging`` for why that thread must not use page
    numbers.
    """

    results = serializers.ListField(child=serializers.DictField())
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    count = serializers.IntegerField(allow_null=True)


def _share_payload(message: DirectMessage) -> dict[str, Any] | None:
    """Build the share sub-payload for a message, or None when it carries no share.

    Args:
        message: The message whose attached share to describe.

    Returns:
        A ``DirectMessageShareSerializer``-shaped dict, or None.
    """
    share = getattr(message, "share", None)
    if share is None:
        return None
    trip = share.trip
    recommended = share.recommended_profile
    return {
        "kind": share.kind,
        "pin_share_id": share.pin_share_id,
        "trip_slug": trip.slug if trip is not None else None,
        "recommended_profile_slug": (recommended.slug or None) if recommended is not None else None,
        "revoked": share.revoked_at is not None,
    }


def build_direct_message_payload(message: DirectMessage, viewer: Profile) -> dict[str, Any]:
    """Render one direct message for one viewer.

    Starts from ``services.direct_messages.serialize_direct_message`` rather
    than reading the model directly, so the identity masking that function
    applies (the sender's name resolved through *this viewer's* visibility)
    cannot be skipped here. The API-specific fields are layered on top.

    Args:
        message: The message to render.
        viewer: The profile the payload is for. Drives both identity masking
            and the tombstone/expiry decision.

    Returns:
        A ``DirectMessageSerializer``-shaped dict.
    """
    base = serialize_direct_message(message, viewer=viewer)
    tombstone = message.tombstone_text_for(viewer.pk)
    share = _share_payload(message)

    payload: dict[str, Any] = {
        "id": message.pk,
        "sender_slug": base["sender_slug"],
        "recipient_slug": base["recipient_slug"],
        "sender_name": base["sender_name"],
        "created": message.created,
        "read_at": message.read_at,
        "reply_to_id": message.reply_to_id,
        "sender_delete_after": message.sender_delete_after,
        "expires_for_recipient": message.is_expired_for_recipient,
        "tombstone": tombstone,
        "reactions": reaction_summary(message),
        "share": share,
        "pin_share_id": share["pin_share_id"] if share is not None else None,
        "markup_map_uuid": base["markup_map_uuid"],
        "map_removed": message.map_removed,
        "image_ids": [image["id"] for image in base["images"]],
        "body": base["body"],
        "ciphertext": base["ciphertext"],
        "nonce": base["nonce"],
        "key_version": base["key_version"],
        "is_encrypted": message.is_encrypted,
    }

    if tombstone is not None:
        # The viewer may no longer see this content (sender deleted it for
        # everyone, or the sender's retention window elapsed). Blank the
        # payload rather than relying on the client to honor `tombstone` -
        # a client that ignored it would otherwise still receive, and could
        # still display, content the sender has already revoked.
        payload["body"] = ""
        payload["ciphertext"] = ""
        payload["nonce"] = ""
        payload["key_version"] = 0
        payload["image_ids"] = []
        payload["markup_map_uuid"] = None

    return payload


def build_group_message_payload(message: GroupMessage, viewer: Profile) -> dict[str, Any]:
    """Render one group message into the same envelope as a direct message.

    One message shape across both conversation kinds keeps clients from
    needing two renderers for what a user experiences as the same thing. The
    fields a group message has no analogue for are reported as the empty/None
    value rather than omitted, so the shape stays stable: ``recipient_slug``
    (a group message has no single recipient), ``read_at``/``reply_to_id``/
    ``sender_delete_after``/``markup_map_uuid`` (group messages carry none of
    these today).

    Two fields that *are* populated, and were not when this function was
    written:

    - ``reactions`` - group messages became reactable when ``Reaction`` grew
      its ``group_message`` foreign key, and the summary is the identical
      shape a direct message reports, from the identical function.
    - ``pin_share_id`` - a pin shared into a group creates one ``PinShare`` per
      recipient (see ``services.group_chats.share_pin_in_group_message``), so
      unlike a direct message there is no single share object to nest under
      ``share``; the viewer's *own* row is resolved here. Without it a group
      recipient can see the share card but has no id to POST to
      ``/pin-shares/{share_id}/respond/``, which is to say the share is
      undismissable and unacceptable from a mobile client.

    ``share`` itself stays null: its ``kind``/``revoked`` fields describe a
    ``DirectMessageShare``, which a group message does not have, and inventing
    plausible-looking values for them would be worse than reporting nothing.

    Args:
        message: The group message to render.
        viewer: The member the payload is for; drives identity masking, the
            tombstone decision, and which recipient's share row is reported.

    Returns:
        A ``DirectMessageSerializer``-shaped dict.
    """
    from urbanlens.dashboard.services.group_chats import serialize_group_message

    base = serialize_group_message(message, viewer=viewer)
    tombstone = message.tombstone_text_for(viewer.pk)
    # Iterates the (usually prefetched) shares relation rather than filtering,
    # so rendering a whole thread page stays N+1-free.
    viewer_share = message.share_for(viewer.pk)

    payload: dict[str, Any] = {
        "id": message.pk,
        "sender_slug": base["sender_slug"],
        "recipient_slug": "",
        "sender_name": base["sender_name"],
        "created": message.created,
        "read_at": None,
        "reply_to_id": None,
        "sender_delete_after": "",
        "expires_for_recipient": False,
        "tombstone": tombstone,
        "reactions": reaction_summary(message),
        "share": None,
        "pin_share_id": viewer_share.pin_share_id if viewer_share is not None else None,
        "markup_map_uuid": None,
        "map_removed": False,
        "image_ids": [],
        "body": base["body"],
        "ciphertext": base["ciphertext"],
        "nonce": base["nonce"],
        "key_version": base["key_version"],
        "is_encrypted": message.is_encrypted,
    }

    if tombstone is not None:
        payload["body"] = ""
        payload["ciphertext"] = ""
        payload["nonce"] = ""
        payload["key_version"] = 0

    return payload


def build_conversation_payload(conversation: dict[str, Any], viewer: Profile) -> dict[str, Any]:
    """Normalize one ``all_conversations_for`` row into the unified inbox shape.

    Args:
        conversation: A row from ``services.direct_messages.conversations_for``
            (``kind="dm"``) or
            ``services.group_chats.group_conversations_for`` (``kind="group"``).
            Their shapes differ; this is where that difference stops.
        viewer: The profile whose inbox this is.

    Returns:
        A ``ConversationSerializer``-shaped dict.
    """
    last_message = conversation.get("last_message")

    if conversation["kind"] == "group":
        group: GroupChat = conversation["group"]
        creator = group.creator
        return {
            "kind": "group",
            "peer_slug": None,
            "group_uuid": str(group.uuid),
            # Null both when this is a DM and when the creator deleted their
            # account (the FK is SET_NULL) - a group outliving its creator has
            # nobody who can manage its membership, which clients must be able
            # to represent rather than crash on.
            "creator_slug": (creator.slug or None) if creator is not None else None,
            "display_name": group.name,
            "display_avatar_url": None,
            "is_anonymized": False,
            "unread_count": conversation["unread_count"],
            "member_count": conversation["member_count"],
            "is_muted": conversation["is_muted"],
            "last_activity": conversation["last_activity"],
            "last_message": build_group_message_payload(last_message, viewer) if last_message is not None else None,
        }

    partner: Profile = conversation["partner"]
    return {
        "kind": "dm",
        "peer_slug": partner.slug or None,
        "group_uuid": None,
        "creator_slug": None,
        "display_name": conversation["display_name"],
        "display_avatar_url": conversation.get("display_avatar_url"),
        "is_anonymized": conversation["is_anonymized"],
        "unread_count": conversation["unread_count"],
        "member_count": None,
        "is_muted": conversation["is_muted"],
        "last_activity": conversation["last_activity"],
        "last_message": build_direct_message_payload(last_message, viewer) if last_message is not None else None,
    }
