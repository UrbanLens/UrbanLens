"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    model.py                                                                                             *
*        Path:    /dashboard/models/friendship/model.py                                                                *
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
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations

import logging

from django.db.models import CASCADE, CharField, ForeignKey

from urbanlens.dashboard.models.abstract import Model, TextChoices
from urbanlens.dashboard.models.friendship.queryset import Manager
from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


class Friendship(Model):
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
            return status in [cls.DECLINED, cls.REMOVED, cls.BLOCKED, cls.MUTED]

        @classmethod
        def can_request(cls, status: str) -> bool:
            return status in [cls.DECLINED, cls.REMOVED]

    class FriendshipType(TextChoices):
        FOLLOWING = "Following", "Following"
        FRIEND = "Friend", "Friend"
        CLOSE_FRIEND = "Close Friend", "Close Friend"

    class Permission(TextChoices):
        SEND_MESSAGE = "Send Message", "Send Message"
        INVITE_TO_EVENTS = "Invite to Events", "Invite to Events"
        SHARE_LOCATIONS = "Share Pins", "Share Pins"
        VIEW_PROFILE = "View Profile", "View Profile"
        VIEW_FRIENDS = "View Friends", "View Friends"
        VIEW_TRIPS = "View Trips", "View Trips"

    from_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="friendships",
    )
    to_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="friends_to_me",
    )

    status = CharField(max_length=10, choices=FriendshipStatus.choices)
    relationship_type = CharField(max_length=12, choices=FriendshipType.choices)
    permissions = CharField(max_length=16, choices=Permission.choices)

    objects = Manager()

    @classmethod
    def request(cls, from_profile: Profile | int, to_profile: Profile | int, relationship_type: str = FriendshipType.FRIEND) -> Friendship | None:
        """
        Create a new friendship request.
        """
        # Check if a request has already been made
        friendship = cls.objects.between(from_profile, to_profile)
        if friendship:
            # Check if we can make another request
            if not cls.FriendshipStatus.can_request(friendship.status):
                logger.warning("Cannot request another friendship")
                return None

            # Update the status to requested
            friendship.status = cls.FriendshipStatus.REQUESTED

        else:
            if isinstance(from_profile, int):
                from_profile = Profile.objects.get(pk=from_profile)
            if isinstance(to_profile, int):
                to_profile = Profile.objects.get(pk=to_profile)

            if not from_profile or not to_profile:
                logger.warning("Could not find profiles")
                raise ValueError("Could not find profiles")

            friendship = cls.objects.create(
                from_profile=from_profile,
                to_profile=to_profile,
                relationship_type=relationship_type,
                status=cls.FriendshipStatus.REQUESTED,
            )

        friendship.save()
        return friendship

    def accept(self):
        """
        Accept a friendship request.
        """
        self.status = self.FriendshipStatus.ACCEPTED
        self.save()

    def decline(self):
        """
        Decline a friendship request.
        """
        self.status = self.FriendshipStatus.DECLINED
        self.save()

    def remove(self):
        """
        Remove a friendship.
        """
        self.status = self.FriendshipStatus.REMOVED
        self.save()

    @classmethod
    def block(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Block a profile.
        """
        friendship = cls.objects.between(from_profile, to_profile)
        if friendship:
            friendship.status = cls.FriendshipStatus.BLOCKED
            friendship.save()
            return friendship

        # Create a new friendship with status blocked
        if isinstance(from_profile, int):
            from_profile = Profile.objects.get(pk=from_profile)
        if isinstance(to_profile, int):
            to_profile = Profile.objects.get(pk=to_profile)

        if not from_profile or not to_profile:
            logger.warning("Could not find profiles")
            raise ValueError("Could not find profiles")

        friendship = cls.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=cls.FriendshipStatus.BLOCKED,
        )

        return friendship

    @classmethod
    def mute(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Mute a profile.
        """
        friendship = cls.objects.between(from_profile, to_profile)
        if friendship:
            friendship.status = cls.FriendshipStatus.MUTED
            friendship.save()
            return friendship

        # Create a new friendship with status muted
        if isinstance(from_profile, int):
            from_profile = Profile.objects.get(pk=from_profile)
        if isinstance(to_profile, int):
            to_profile = Profile.objects.get(pk=to_profile)

        if not from_profile or not to_profile:
            logger.warning("Could not find profiles")
            raise ValueError("Could not find profiles")

        friendship = cls.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=cls.FriendshipStatus.MUTED,
        )

        return friendship

    def __str__(self):
        return f"{self.from_profile.username} to {self.to_profile.username} - {self.relationship_type} - {self.status}"

    class Meta(Model.Meta):
        db_table = "dashboard_friendships"
        unique_together = ("from_profile", "to_profile")
