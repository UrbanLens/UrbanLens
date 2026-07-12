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
from urbanlens.dashboard.services.direct_messages import create_direct_message

if TYPE_CHECKING:
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip

#: How long a recipient of an `@friend` recommendation can view the
#: recommended profile as though they were friends, to decide whether to
#: actually connect.
FRIEND_RECOMMENDATION_ACCESS_DURATION = datetime.timedelta(days=1)


def share_pin_in_message(sender: Profile, recipient: Profile, pin: Pin, body: str, *, markup_map_uuid: str | None = None) -> DirectMessage:
    """Share `pin` with `recipient` as a chat message carrying a PinShare.

    Args:
        sender: The profile sharing the pin (must own it and be connected to `recipient`).
        recipient: The conversation partner receiving the share.
        pin: The pin being shared.
        body: Message text accompanying the share.
        markup_map_uuid: Optional customized map to attach (see `create_direct_message`).

    Returns:
        The newly created DirectMessage.

    Raises:
        PermissionError: If sender/recipient aren't connected friends, or
            messaging is otherwise not permitted.
        ValueError: Propagated from `create_direct_message` for bad input.
    """
    from urbanlens.dashboard.services.pin_sharing import create_pin_share

    pin_share = create_pin_share(sender, recipient, pin)
    message = create_direct_message(sender, recipient, body, markup_map_uuid=markup_map_uuid)
    DirectMessageShare.objects.create(message=message, kind=DirectMessageShareKind.PIN, pin_share=pin_share)
    return message


def invite_to_trip_in_message(sender: Profile, recipient: Profile, trip: Trip, body: str) -> DirectMessage:
    """Invite `recipient` to `trip` as a chat message carrying the invite.

    Args:
        sender: The profile sending the invite (must already be a trip member,
            and connected to `recipient`).
        recipient: The conversation partner being invited.
        trip: The trip to invite them to.
        body: Message text accompanying the invite.

    Returns:
        The newly created DirectMessage.

    Raises:
        PermissionError: If sender/recipient aren't connected, or sender isn't
            a member of `trip`.
        ValueError: Propagated from `create_direct_message` for bad input.
    """
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.trips.model import TripMembership

    if not are_connections(sender, recipient):
        raise PermissionError("You can only invite connected friends to a trip.")
    if not trip.memberships.filter(profile=sender).exists():
        raise PermissionError("You aren't a member of that trip.")

    membership, _created = TripMembership.objects.get_or_create(trip=trip, profile=recipient)
    message = create_direct_message(sender, recipient, body)
    DirectMessageShare.objects.create(message=message, kind=DirectMessageShareKind.TRIP, trip=trip, trip_membership=membership)

    pref = getattr(recipient.notification_preferences, "added_to_trip", DeliveryPreference.SITE)
    if pref != DeliveryPreference.NONE:
        from django.urls import reverse

        NotificationLog.objects.create(
            profile=recipient,
            source_profile=sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.ADDED_TO_TRIP,
            title="Trip invitation",
            message=f'{sender.username} invited you to join "{trip.name}".',
            url=reverse("trips.detail", kwargs={"trip_uuid": trip.uuid}),
        )
    return message


def recommend_friend_in_message(sender: Profile, recipient: Profile, recommended: Profile, body: str) -> DirectMessage:
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

    Returns:
        The newly created DirectMessage.

    Raises:
        PermissionError: If `recommended` isn't one of sender's connections,
            or has turned off friend recommendations.
        ValueError: Propagated from `create_direct_message` for bad input.
    """
    if recommended.pk in (recipient.pk, sender.pk):
        raise PermissionError("Choose a different friend to recommend.")
    if recommended not in get_connections(sender):
        raise PermissionError("You can only recommend your own connected friends.")
    if not recommended.allow_friend_recommendations:
        raise PermissionError(f"{recommended.username} doesn't allow friend recommendations.")

    message = create_direct_message(sender, recipient, body)
    DirectMessageShare.objects.create(message=message, kind=DirectMessageShareKind.FRIEND, recommended_profile=recommended)
    DirectMessageTemporaryAccess.objects.create(
        profile=recommended,
        granted_to=recipient,
        expires_at=timezone.now() + FRIEND_RECOMMENDATION_ACCESS_DURATION,
    )
    return message
