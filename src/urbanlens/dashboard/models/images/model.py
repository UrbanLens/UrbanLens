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
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations

from django.db.models import CASCADE, ForeignKey, ImageField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.images.queryset import ImageManager


class Image(abstract.Model):
    """
    Records image data.
    """
    image = ImageField()
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="images",
    )

    objects = ImageManager()

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_images"
        get_latest_by = "updated"
