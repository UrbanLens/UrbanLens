from __future__ import annotations

from django.db.models import TextChoices


class FriendshipStatus(TextChoices):
    REQUESTED = "Requested", "Requested"
    ACCEPTED = "Accepted", "Accepted"
    DECLINED = "Declined", "Declined"
    REMOVED = "Removed", "Removed"
    MUTED = "Muted", "Muted"
    BLOCKED = "Blocked", "Blocked"

    @classmethod
    def is_friend(cls, status: str) -> bool:
        return status == cls.ACCEPTED

    @classmethod
    def rejected(cls, status: str) -> bool:
        return status in {cls.DECLINED, cls.REMOVED, cls.BLOCKED, cls.MUTED}

    @classmethod
    def can_request(cls, status: str) -> bool:
        return status in {cls.DECLINED, cls.REMOVED}
