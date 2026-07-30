from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class FriendshipStatus(TextChoices):
    """What kind of relationship two profiles have - and nothing else.

    ``MUTED`` is retained but **deprecated and never written**. Mute is a
    notification preference, not a relationship state, and it now lives on
    ``Friendship.muted``; see that model's docstring for the data-integrity
    bug that conflating the two caused (muting an accepted friend overwrote
    ``ACCEPTED``, so ``Profile.are_friends`` - and therefore every visibility
    gate in the app - stopped seeing them as friends). The value stays in the
    enum, and in ``rejected()``, purely as defence in depth: it keeps the
    stored wire vocabulary stable for API clients, and it means that any row
    the data migration somehow missed still fails closed rather than being
    treated as an unknown status.
    """

    PENDING = "Pending", "Pending"
    REQUESTED = "Requested", "Requested"
    ACCEPTED = "Accepted", "Accepted"
    DECLINED = "Declined", "Declined"
    REMOVED = "Removed", "Removed"
    MUTED = "Muted", "Muted"
    BLOCKED = "Blocked", "Blocked"
    IGNORED = "Ignored", "Ignored"

    @classmethod
    def is_friend(cls, status: str) -> bool:
        return status == cls.ACCEPTED

    @classmethod
    def rejected(cls, status: str) -> bool:
        return status in {cls.DECLINED, cls.REMOVED, cls.BLOCKED, cls.MUTED, cls.IGNORED}

    @classmethod
    def can_request(cls, status: str) -> bool:
        # IGNORED intentionally excluded: the button stays unavailable
        return status in {cls.DECLINED, cls.REMOVED}


class FriendshipType(TextChoices):
    ENCOUNTERED = "Encountered", "Encountered"
    CONNECTED = "Connected", "Connected"
    FRIEND = "Friend", "Friend"
    CLOSE_FRIEND = "Close Friend", "Close Friend"


class Permission(TextChoices):
    SEND_MESSAGE = "Send Message", "Send Message"
    INVITE_TO_EVENTS = "Invite to Events", "Invite to Events"
    SHARE_LOCATIONS = "Share Pins", "Share Pins"
    VIEW_PROFILE = "View Profile", "View Profile"
    VIEW_FRIENDS = "View Friends", "View Friends"
    VIEW_TRIPS = "View Trips", "View Trips"
