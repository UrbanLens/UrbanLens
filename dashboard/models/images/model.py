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
*        Path:    /dashboard/models/images/model.py                                                                    *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations
from django.db.models import ImageField, CASCADE
from django.db.models import ForeignKey
from dashboard.models import abstract
from dashboard.models.images.queryset import ImageManager

class Image(abstract.Model):
    """
    Records image data.
    """
    image = ImageField()
    location = ForeignKey(
        'dashboard.Location',
        on_delete=CASCADE,
        related_name='images'
    )

    objects = ImageManager()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_images'
        get_latest_by = 'updated'
