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
*        Copyright (c) 2024 Urban Lens                                                                                 *
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
from django.db.models import OneToOneField, CharField, ManyToManyField, TextField, DateField
from dashboard.models import abstract
from dashboard.models.profile.queryset import Manager

class Profile(abstract.Model):
    avatar = ImageField()
    instagram = CharField(max_length=255, null=True, blank=True)
    discord = CharField(max_length=255, null=True, blank=True)
    bio = TextField(null=True, blank=True)
    location = CharField(max_length=255, null=True, blank=True)
    birth_date = DateField(null=True, blank=True)
    started_exploring = DateField(null=True, blank=True)

    user = OneToOneField(
        User,
        on_delete=CASCADE
    )

    objects = Manager()

    @property
    def username(self):
        return self.user.username

    @property
    def email(self):
        return self.user.email

    @property
    def first_name(self):
        return self.user.first_name

    @property
    def last_name(self):
        return self.user.last_name

    @property
    def full_name(self):
        return self.user.get_full_name()

    def __str__(self):
        return self.username

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_profiles'

        indexes = [
            Index(fields=['user']),
        ]
