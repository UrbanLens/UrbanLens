# Generic imports
from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class NotificationType(TextChoices):
    TRIP_UPDATED = "trip_updated", "Trip Updated"
    FRIEND_REQUEST = "friend_request", "New Friend Request"
    MESSAGE = "message", "New Message"
    COMMENT_REPLY = "comment_reply", "Reply to Comment"
    COMMENT_LIKED = "comment_liked", "Comment Likes"
    FRIEND_ACCEPTED = "friend_accepted", "Friend Request Accepted"
    ADDED_TO_TRIP = "added_to_trip", "Trip Invitation"
    WIKI_UPDATED = "wiki_updated", "Community Wiki Updated"
    PIN_SHARED = "pin_shared", "Pin Shared"
    ERROR = "error", "Error"
    WARNING = "warning", "Warning"
    INFO = "info", "Info"
