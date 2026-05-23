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

from django.contrib.auth.models import User
from django.db.models import CASCADE, BooleanField, CharField, DateField, ImageField, Index, OneToOneField, TextChoices, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.profile.queryset import Manager


class VisibilityChoice(TextChoices):
    """Who can see a particular piece of profile data."""
    ONLY_ME = "only_me", "Only Me"
    FRIENDS = "friends", "Friends Only"
    COMMON_LOCATIONS = "common_locations", "People with Common Locations"
    EVERYONE = "everyone", "Everyone"


class Profile(abstract.Model):
    avatar = ImageField(upload_to="avatars/", null=True, blank=True)
    bio = TextField(null=True, blank=True)
    area = CharField(max_length=255, null=True, blank=True)
    birth_date = DateField(null=True, blank=True)
    started_exploring = DateField(null=True, blank=True)

    # Privacy settings
    profile_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.EVERYONE,
    )
    comment_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.EVERYONE,
    )
    allow_friend_requests = BooleanField(default=True)

    # Style preferences
    dark_mode = BooleanField(default=False)

    user = OneToOneField(
        User,
        on_delete=CASCADE,
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
        db_table = "dashboard_profiles"

        indexes = [
            Index(fields=["user"]),
        ]
