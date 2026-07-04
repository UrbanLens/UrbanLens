from __future__ import annotations

from django.db.models import TextChoices


class FriendshipStatus(TextChoices):
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
