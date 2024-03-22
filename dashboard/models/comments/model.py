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
*        Path:    /dashboard/models/comments/model.py                                                                  *
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
from django.db.models import CASCADE, CharField, ForeignKey
from dashboard.models import abstract
from dashboard.models.comments.queryset import CommentManager

class Comment(abstract.Model):
    """
    Records comment data.
    """
    text = CharField(max_length=500)

    location = ForeignKey(
        'dashboard.Location',
        on_delete=CASCADE,
        related_name='comments'
    )
    profile = ForeignKey(
        'dashboard.Profile',
        on_delete=CASCADE,
        related_name='comments'
    )

    objects = CommentManager()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_comments'
        get_latest_by = 'updated'
