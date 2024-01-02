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
*        Path:    /dashboard/models/profile/model.py                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
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
from django.db.models import ImageField
from django.db.models import OneToOneField, CharField
from dashboard.models import abstract
from dashboard.models.profile.queryset import Manager


class Profile(abstract.Model):
    avatar = ImageField()
    instagram = CharField(max_length=255, null=True, blank=True)
    discord = CharField(max_length=255, null=True, blank=True)

    user = OneToOneField(
        User, 
        on_delete=CASCADE
    )

    objects = Manager()

    def __str__(self):
        return self.user.username

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_profiles'

        indexes = [
            Index(fields=['user']),
        ]
