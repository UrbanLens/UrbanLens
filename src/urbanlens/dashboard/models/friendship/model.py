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
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.queryset import Manager
from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


class Friendship(Model):
    status = CharField(max_length=10, choices=FriendshipStatus.choices)
    relationship_type = CharField(max_length=12, choices=FriendshipType.choices)
    permissions = CharField(max_length=16, choices=Permission.choices)

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

    objects = Manager()

    @classmethod
    def request(
        cls,
        from_profile: Profile | int,
        to_profile: Profile | int,
        relationship_type: str = FriendshipType.FRIEND,
    ) -> Friendship | None:
        """
        Create a new friendship request.
        """
        # Check if a request has already been made
        friendship = cls.objects.all().between(from_profile, to_profile)
        if friendship:
            # Check if we can make another request
            if not FriendshipStatus.can_request(friendship.status):
                logger.warning("Cannot request another friendship")
                return None

            # Update the status to requested
            friendship.status = FriendshipStatus.REQUESTED

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
                status=FriendshipStatus.REQUESTED,
            )

        friendship.save()
        return friendship

    def accept(self):
        """
        Accept a friendship request.
        """
        self.status = FriendshipStatus.ACCEPTED
        self.save()

    def decline(self):
        """
        Decline a friendship request.
        """
        self.status = FriendshipStatus.DECLINED
        self.save()

    def remove(self):
        """
        Remove a friendship.
        """
        self.status = FriendshipStatus.REMOVED
        self.save()

    @classmethod
    def block(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Block a profile.
        """
        friendship = cls.objects.all().between(from_profile, to_profile)
        if friendship:
            friendship.status = FriendshipStatus.BLOCKED
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

        return cls.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=FriendshipStatus.BLOCKED,
        )

    @classmethod
    def mute(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Mute a profile.
        """
        friendship = cls.objects.all().between(from_profile, to_profile)
        if friendship:
            friendship.status = FriendshipStatus.MUTED
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

        return cls.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=FriendshipStatus.MUTED,
        )

    def __str__(self):
        return f"{self.from_profile.username} to {self.to_profile.username} - {self.relationship_type} - {self.status}"

    class Meta(Model.Meta):
        db_table = "dashboard_friendships"
        unique_together = ("from_profile", "to_profile")
