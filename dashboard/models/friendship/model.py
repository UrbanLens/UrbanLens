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
*        Path:    /model.py                                                                                            *
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
from __future__ import annotations
from django.db.models import CASCADE
from django.contrib.auth.models import User
from djangofoundry.models import ForeignKey
from dashboard.models.abstract.model import Model
from dashboard.models.friendship.queryset import Manager

class Friendship(Model):
    user = ForeignKey(
        User, 
        on_delete=CASCADE, 
        related_name='user'
    )
    friend = ForeignKey(
        User, 
        on_delete=CASCADE, 
        related_name='friend'
    )

    objects = Manager()

    class Meta(Model.Meta):
        db_table = 'dashboard_friendships'
        unique_together = ('user', 'friend')
