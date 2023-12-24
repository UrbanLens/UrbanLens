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
*        Path:    /queryset.py                                                                                         *
*        Project: friendship                                                                                           *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from django.db.models import QuerySet, Manager
from dashboard.models.friendship.model import Friendship

class FriendshipQuerySet(QuerySet):
    pass

class FriendshipManager(Manager.from_queryset(FriendshipQuerySet)):
    def get_queryset(self):
        return FriendshipQuerySet(self.model, using=self._db)

Friendship.objects = FriendshipManager()
