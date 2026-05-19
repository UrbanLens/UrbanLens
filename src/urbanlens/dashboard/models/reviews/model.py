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
*        Path:    /dashboard/models/reviews/model.py                                                                   *
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
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import CASCADE
from django.db.models.fields import IntegerField, TextField
from django.db.models.fields.related import ForeignKey

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.queryset import Manager


class Review(abstract.Model):
    rating = IntegerField(validators=[MinValueValidator(0), MaxValueValidator(5)])
    review = TextField()

    user = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name="reviews",
    )
    pin = ForeignKey(
        Pin,
        on_delete=CASCADE,
        related_name="reviews",
    )

    objects = Manager()

    class Meta(abstract.Model.Meta):
        unique_together = ("user", "pin")
        get_latest_by = "created"
