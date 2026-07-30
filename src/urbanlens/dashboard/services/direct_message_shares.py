"""`@pin` / `@trip` / `@friend` sharing embedded in direct messages.

Each function creates the underlying share/invite/recommendation through the
same code the standalone features use (pin sharing, trip membership,
friendship), sends the chat message via `create_direct_message`, and wraps
the two together in a `DirectMessageShare` so deleting the message can revoke
the offer later (see `DirectMessageShare.revoke`).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.direct_messages.meta import DirectMessageShareKind
from urbanlens.dashboard.models.direct_messages.share import DirectMessageShare
from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess
from urbanlens.dashboard.services.connections import are_connections, get_connections
from urbanlens.dashboard.services.direct_messages import broadcast_direct_message, create_direct_message

if TYPE_CHECKING:
    from uuid import UUID

    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip

#: How long a recipient of an `@friend` recommendation can view the
#: recommended profile as though they were friends, to decide whether to
#: actually connect.
FRIEND_RECOMMENDATION_ACCESS_DURATION = datetime.timedelta(days=1)


class ShareTargetNotFoundError(LookupError):
    """A share referenced a pin/trip/profile the sender can't address.

    Carried as its own type rather than a bare `LookupError` so an API layer
    can map it to 404 without also swallowing genuine lookup bugs.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class ShareTargetPermissionError(PermissionError):
    """A share was refused because of who the sender or target is.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class ShareValidationError(ValueError):
    """A share request itself was malformed.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def send_message_with_share(
    sender: Profile,
    recipient: Profile,
    body: str,
    *,
    shared_pin_slug: str | None = None,
    shared_trip_slug: str | None = None,
    shared_profile_slug: str | None = None,
    markup_map_uuid: str | None = None,
    ciphertext: str = "",
    nonce: str = "",
    key_version: int = 0,
    reply_to_id: int | None = None,
    image_ids: list[int] | None = None,
    client_uuid: UUID | None = None,
) -> DirectMessage:
    """Send one direct message, resolving and attaching an optional `@`-share.

    The single entry point an API layer should use for "send a message that
    may carry a share". Resolution of the share reference happens *here*, not
    in the caller, and each kind is dispatched to the existing service that
    already knows how to create it correctly.

    That indirection is the whole point for pins. A pin share is not just a
    row: `share_pin_in_message` -> `create_pin_share` ->
    `resolve_and_stamp_origin_share` + `record_share_exposure` is what keeps
    the `LocationExposure` provenance chain intact, so that a location's
    re-share history stays traceable back to whoever first exposed it.
    Constructing a `PinShare` or a `DirectMessageShare(kind=PIN)` directly
    from a view would produce a share that *works* - the recipient sees the
    card, can accept it - while silently recording no exposure at all. There
    is no error to notice; the chain just quietly has a hole in it. Never do
    it. Always come through here.

    Args:
        sender: The sending profile.
        recipient: The conversation partner.
        body: Plaintext message text (blank when sending `ciphertext`).
        shared_pin_slug: Slug (or uuid) of one of the *sender's own* pins to
            share. Resolved against the sender's pins only.
        shared_trip_slug: Slug of a trip to invite `recipient` to.
        shared_profile_slug: Slug of one of the sender's connections to
            recommend.
        markup_map_uuid: UUID of a `MarkupMap` to attach. Combined with
            `shared_pin_slug` this is a customized pin share; on its own it is
            a plain map attachment.
        ciphertext: End-to-end encrypted note, in place of `body`.
        nonce: Base64 nonce for `ciphertext`.
        key_version: `ConversationKey.version` that encrypted `ciphertext`.
        reply_to_id: PK of an earlier message in this conversation to quote.
        image_ids: PKs of the sender's own images to attach.
        client_uuid: Caller-generated idempotency key. Checked *before* any
            share is resolved or created, so a retried share send cannot
            create a second `PinShare` (and a second `LocationExposure`) for a
            message that already exists.

    Returns:
        The created (or, on an idempotent replay, the pre-existing) DirectMessage.

    Raises:
        ShareTargetNotFoundError: The referenced pin/trip/profile doesn't
            exist or isn't one the sender may share.
        ValueError: More than one share field was given, or `create_direct_message`
            rejected the content.
        PermissionError: Propagated from the underlying share service (not
            connected, trip non-membership, recommendations disabled, ...).
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage as DirectMessageModel

    provided = [field for field in (shared_pin_slug, shared_trip_slug, shared_profile_slug) if field]
    if len(provided) > 1:
        raise ShareValidationError("A message can carry only one share.")

    # Before anything is resolved or created: a replay must not re-run the
    # share side effects, which are not themselves idempotent.
    if client_uuid is not None:
        replayed = DirectMessageModel.objects.filter(sender=sender, client_uuid=client_uuid).first()
        if replayed is not None:
            return replayed

    if shared_pin_slug:
        from urbanlens.dashboard.models.pin.model import Pin as PinModel

        pin = PinModel.objects.slug_or_uuid(shared_pin_slug).filter(profile=sender).first()
        if pin is None:
            raise ShareTargetNotFoundError("No such pin.")
        return share_pin_in_message(
            sender,
            recipient,
            pin,
            body,
            markup_map_uuid=markup_map_uuid,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            reply_to_id=reply_to_id,
            image_ids=image_ids,
            client_uuid=client_uuid,
        )

    if shared_trip_slug:
        from urbanlens.dashboard.models.trips.model import Trip as TripModel

        # Membership is enforced by invite_to_trip_in_message; this lookup is
        # deliberately not scoped to the sender's trips, so "you aren't a
        # member of that trip" stays a PermissionError rather than being
        # flattened into a 404.
        trip = TripModel.objects.filter(slug=shared_trip_slug).first()
        if trip is None:
            raise ShareTargetNotFoundError("No such trip.")
        return invite_to_trip_in_message(
            sender,
            recipient,
            trip,
            body,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            reply_to_id=reply_to_id,
            client_uuid=client_uuid,
        )

    if shared_profile_slug:
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        recommended = ProfileModel.objects.filter(slug=shared_profile_slug).first()
        if recommended is None:
            raise ShareTargetNotFoundError("No such profile.")
        return recommend_friend_in_message(
            sender,
            recipient,
            recommended,
            body,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            reply_to_id=reply_to_id,
            client_uuid=client_uuid,
        )

    # No share - a plain message, possibly with a map attached. Note that a
    # MarkupMap attached without a pin records no LocationExposure even though
    # it can depict pin locations; that gap predates this function and exists
    # in the web composer too (docs/PROBLEMS.md: "markup-map attachments
    # bypass share provenance").
    return create_direct_message(
        sender,
        recipient,
        body,
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=key_version,
        image_ids=image_ids,
        markup_map_uuid=markup_map_uuid,
        reply_to_id=reply_to_id,
        client_uuid=client_uuid,
    )


def share_pin_in_message(
    sender: Profile,
    recipient: Profile,
    pin: Pin,
    body: str,
    *,
    markup_map_uuid: str | None = None,
    ciphertext: str = "",
    nonce: str = "",
    key_version: int = 0,
    reply_to_id: int | None = None,
    image_ids: list[int] | None = None,
    client_uuid: UUID | None = None,
) -> DirectMessage:
    """Share `pin` with `recipient` as a chat message carrying a PinShare.

    Args:
        sender: The profile sharing the pin (must own it and be connected to `recipient`).
        recipient: The conversation partner receiving the share.
        pin: The pin being shared.
        body: Message text accompanying the share.
        markup_map_uuid: Optional customized map to attach (see `create_direct_message`).
        ciphertext: Optional end-to-end encrypted note accompanying the share,
            in place of `body`. Only the *note* is encrypted - the
            `DirectMessageShare` row itself is server-visible metadata by
            design, because the server has to resolve and revoke the offer it
            represents. Do not attempt to encrypt it.
        nonce: Base64 nonce for `ciphertext`.
        key_version: `ConversationKey.version` that encrypted `ciphertext`.
        reply_to_id: PK of an earlier message in this conversation to quote.
        image_ids: PKs of the sender's own images to attach.
        client_uuid: Caller-generated idempotency key (see `create_direct_message`).

    Returns:
        The newly created DirectMessage.

    Raises:
        PermissionError: If sender/recipient aren't connected friends, or
            messaging is otherwise not permitted.
        ValueError: Propagated from `create_direct_message` for bad input.
    """
    from django.db import IntegrityError, transaction

    from urbanlens.dashboard.models.direct_messages.model import DirectMessage as DirectMessageModel
    from urbanlens.dashboard.services.pin_sharing import create_pin_share

    # Replay is settled before the PinShare is created, not after. Deferring to
    # create_direct_message's own idempotency check was too late: create_pin_share
    # had already run, so a retry minted a *second* PinShare - and a second
    # LocationExposure, the provenance row resolve_origin_share walks - before
    # the message call returned the original. The share row insert below then
    # collided with the winner's (one-to-one shares are unique per message) and
    # raised an uncaught IntegrityError, so a send the caller was promised was
    # idempotent answered 500 while having already corrupted the exposure chain.
    if client_uuid is not None:
        replayed = DirectMessageModel.objects.filter(sender=sender, client_uuid=client_uuid).first()
        if replayed is not None:
            return replayed

    # One transaction: if the message itself is refused (e.g. the recipient's
    # DM visibility rejects this sender despite the friendship), the PinShare
    # and its exposure record must roll back with it - a share offer must
    # never exist without the message that carries it.
    try:
        with transaction.atomic():
            pin_share = create_pin_share(sender, recipient, pin)
            message = create_direct_message(
                sender,
                recipient,
                body,
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=key_version,
                markup_map_uuid=markup_map_uuid,
                reply_to_id=reply_to_id,
                image_ids=image_ids,
                client_uuid=client_uuid,
                defer_broadcast=True,
            )
            DirectMessageShare.objects.create(message=message, kind=DirectMessageShareKind.PIN, pin_share=pin_share)
    except IntegrityError:
        # Two retries raced past the check above. The whole block rolled back,
        # including this call's PinShare and its exposure, so the winner's
        # message is canonical and complete - return it rather than surfacing
        # the collision.
        replayed = DirectMessageModel.objects.filter(sender=sender, client_uuid=client_uuid).first() if client_uuid is not None else None
        if replayed is None:
            raise
        return replayed
    broadcast_direct_message(message)
    return message


def invite_to_trip_in_message(
    sender: Profile,
    recipient: Profile,
    trip: Trip,
    body: str,
    *,
    ciphertext: str = "",
    nonce: str = "",
    key_version: int = 0,
    reply_to_id: int | None = None,
    client_uuid: UUID | None = None,
) -> DirectMessage:
    """Invite `recipient` to `trip` as a chat message carrying the invite.

    Args:
        sender: The profile sending the invite (must already be a trip member,
            and connected to `recipient`).
        recipient: The conversation partner being invited.
        trip: The trip to invite them to.
        body: Message text accompanying the invite.
        ciphertext: Optional end-to-end encrypted note in place of `body`; the
            `DirectMessageShare` row stays server-visible (see
            `share_pin_in_message`).
        nonce: Base64 nonce for `ciphertext`.
        key_version: `ConversationKey.version` that encrypted `ciphertext`.
        reply_to_id: PK of an earlier message in this conversation to quote.
        client_uuid: Caller-generated idempotency key.

    Returns:
        The newly created DirectMessage.

    Raises:
        PermissionError: If sender/recipient aren't connected, or sender isn't
            a member of `trip`.
        ValueError: Propagated from `create_direct_message` for bad input.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.trips.model import TripMembership

    if not are_connections(sender, recipient):
        raise ShareTargetPermissionError("You can only invite connected friends to a trip.")
    if not trip.memberships.filter(profile=sender).exists():
        raise ShareTargetPermissionError("You aren't a member of that trip.")

    # One transaction: if the message itself is refused (e.g. the recipient's
    # DM visibility rejects this sender despite the friendship), the invited
    # TripMembership must roll back with it - a membership must never be
    # created without the recipient ever receiving the invitation.
    with transaction.atomic():
        membership, _created = TripMembership.objects.get_or_create(trip=trip, profile=recipient, defaults={"status": TripMembership.STATUS_INVITED})
        message = create_direct_message(
            sender,
            recipient,
            body,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            reply_to_id=reply_to_id,
            client_uuid=client_uuid,
            defer_broadcast=True,
        )
        DirectMessageShare.objects.create(message=message, kind=DirectMessageShareKind.TRIP, trip=trip, trip_membership=membership)
    broadcast_direct_message(message)

    try:
        pref = recipient.notification_preferences.added_to_trip
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref != DeliveryPreference.NONE:
        from django.urls import reverse

        from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity

        # profile_visibility permits NO_ONE even for accepted friends (see
        # VisibilityChoice's docstring) - being connected doesn't guarantee
        # sender is visible to recipient, so this must still be resolved
        # (and masked if needed) before formatting the stored message text.
        sender_name = resolve_visible_identity(recipient, sender)["display_name"]
        NotificationLog.objects.create(
            profile=recipient,
            source_profile=sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.ADDED_TO_TRIP,
            title="Trip invitation",
            message=f'{sender_name} invited you to join "{trip.name}".',
            url=reverse("trips.detail", kwargs={"trip_slug": trip.slug}),
        )
    return message


def recommend_friend_in_message(
    sender: Profile,
    recipient: Profile,
    recommended: Profile,
    body: str,
    *,
    ciphertext: str = "",
    nonce: str = "",
    key_version: int = 0,
    reply_to_id: int | None = None,
    client_uuid: UUID | None = None,
) -> DirectMessage:
    """Recommend `recommended` (one of sender's own friends) to `recipient` as a chat message.

    Grants `recipient` temporary access to view `recommended`'s profile (as if
    they were already friends) for `FRIEND_RECOMMENDATION_ACCESS_DURATION`, so
    they can decide whether to connect.

    Args:
        sender: The profile making the recommendation.
        recipient: The conversation partner receiving the recommendation.
        recommended: The profile being recommended - must be one of sender's
            own connections and must allow friend recommendations.
        body: Message text accompanying the recommendation.
        ciphertext: Optional end-to-end encrypted note in place of `body`; the
            `DirectMessageShare` row stays server-visible (see
            `share_pin_in_message`).
        nonce: Base64 nonce for `ciphertext`.
        key_version: `ConversationKey.version` that encrypted `ciphertext`.
        reply_to_id: PK of an earlier message in this conversation to quote.
        client_uuid: Caller-generated idempotency key.

    Returns:
        The newly created DirectMessage.

    Raises:
        PermissionError: If `recommended` isn't one of sender's connections,
            or has turned off friend recommendations.
        ValueError: Propagated from `create_direct_message` for bad input.
    """
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    if recommended.pk in (recipient.pk, sender.pk):
        raise ShareTargetPermissionError("Choose a different friend to recommend.")
    if recommended not in get_connections(sender):
        raise ShareTargetPermissionError("You can only recommend your own connected friends.")
    if not recommended.allow_friend_recommendations:
        raise ShareTargetPermissionError(f"{recommended.username} doesn't allow friend recommendations.")
    if ProfileModel.are_blocked(recommended, recipient):
        # A block in either direction vetoes the recommendation - it would
        # otherwise grant the recipient temporary profile access the block
        # exists to prevent. Deliberately the same message as the opt-out
        # above, so the sender cannot distinguish "blocked" from
        # "recommendations disabled".
        raise ShareTargetPermissionError(f"{recommended.username} doesn't allow friend recommendations.")

    from django.db import transaction

    with transaction.atomic():
        message = create_direct_message(
            sender,
            recipient,
            body,
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=key_version,
            reply_to_id=reply_to_id,
            client_uuid=client_uuid,
            defer_broadcast=True,
        )
        DirectMessageShare.objects.create(message=message, kind=DirectMessageShareKind.FRIEND, recommended_profile=recommended)
        DirectMessageTemporaryAccess.objects.create(
            profile=recommended,
            granted_to=recipient,
            expires_at=timezone.now() + FRIEND_RECOMMENDATION_ACCESS_DURATION,
        )
    broadcast_direct_message(message)
    return message
