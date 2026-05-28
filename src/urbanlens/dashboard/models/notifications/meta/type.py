"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    type.py                                                                                              *
*        Path:    /dashboard/models/notifications/meta/type.py                                                         *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class NotificationType(TextChoices):
    TRIP_UPDATED = "trip_updated", "Trip Updated"
    FRIEND_REQUEST = "friend_request", "Friend Request Received"
    MESSAGE = "message", "Message Received"
    COMMENT_REPLY = "comment_reply", "Reply to Comment"
    COMMENT_LIKED = "comment_liked", "Comment Liked"
    FRIEND_ACCEPTED = "friend_accepted", "Friend Request Accepted"
    ADDED_TO_TRIP = "added_to_trip", "Added to Trip"
    WIKI_UPDATED = "wiki_updated", "Community Wiki Updated"
    ERROR = "error", "Error"
    WARNING = "warning", "Warning"
    INFO = "info", "Info"
