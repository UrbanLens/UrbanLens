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
from django.db.models import CASCADE, CharField, ForeignKey
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.comments.queryset import CommentManager

class Comment(abstract.Model):
    """
    Records comment data.
    """
    text = CharField(max_length=500)

    pin = ForeignKey(
        'dashboard.Pin',
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
