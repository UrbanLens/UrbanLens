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
*        Project: profile                                                                                              *
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
from django.contrib.auth.models import User
from django.db.models import CASCADE, Index
from djangofoundry.models import OneToOneField, CharField
from dashboard.models.abstract.model import Model
from dashboard.models.profile.queryset import Manager

from django.db.models import ImageField

class Profile(Model):
    def __str__(self):
        return self.user.username
    avatar = ImageField()
    instagram = CharField(max_length=255, null=True, blank=True)
    discord = CharField(max_length=255, null=True, blank=True)

    user = OneToOneField(
        User, 
        on_delete=CASCADE
    )

    objects = Manager()

    class Meta(Model.Meta):
        db_table = 'dashboard_profiles'

        indexes = [
            Index(fields=['user']),
        ]
