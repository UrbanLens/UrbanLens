"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    queryset.py                                                                                          *
*        Path:    /dashboard/models/friendship/queryset.py                                                             *
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
from typing import TYPE_CHECKING, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.friendship.model import Friendship

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


class QuerySet(abstract.QuerySet):
    def profile(self, profile: Profile | int) -> Self:
        """
        Return a list of all friendships for a given profile.
        """
        if isinstance(profile, int):
            return self.filter(
                Q(from_profile__id=profile) | Q(to_profile__id=profile),
            )

        return self.filter(
            Q(from_profile=profile) | Q(to_profile=profile),
        )

    def between(self, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Return a list of all friendships between two profiles.
        """
        q1 = {}
        q2 = {}

        if isinstance(from_profile, int):
            q1["from_profile__id"] = from_profile
            q2["to_profile__id"] = from_profile
        else:
            q1["from_profile"] = from_profile
            q2["to_profile"] = from_profile

        if isinstance(to_profile, int):
            q1["to_profile__id"] = to_profile
            q2["from_profile__id"] = to_profile
        else:
            q1["to_profile"] = to_profile
            q2["from_profile"] = to_profile

        return self.filter(Q(**q1) | Q(**q2)).get()

    def user(self, user: User) -> Self:
        """
        Return a list of all friendships for a given user.
        """
        return self.filter(
            Q(from_profile__user=user) | Q(to_profile__user=user),
        )

    def status(self, status: str) -> Self:
        """
        Return a list of all friendships with a given status.
        """
        return self.filter(status=status)

    def is_friend(self) -> Self:
        """
        Return a list of all friendships with a status of accepted.
        """
        return self.filter(status=Friendship.FriendshipStatus.ACCEPTED)

    def not_friend(self) -> Self:
        """
        Return a list of all friendships with a status other than accepted.
        """
        return self.exclude(status=Friendship.FriendshipStatus.ACCEPTED)

    def relationship_type(self, relationship_type: str) -> Self:
        """
        Return a list of all friendships with a given type.
        """
        return self.filter(relationship_type=relationship_type)

    def has_permission(self, permission: str) -> Self:
        """
        Return a list of all friendships with a given permission.
        """
        return self.filter(permissions=permission)


class Manager(abstract.Manager.from_queryset(QuerySet)):
    pass
