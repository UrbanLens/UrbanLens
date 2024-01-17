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
from django.contrib.auth.models import User
from django.db.models import CASCADE
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models.fields import IntegerField, TextField
from django.db.models.fields.related import ForeignKey
from dashboard.models import abstract
from dashboard.models.reviews.queryset import Manager
from dashboard.models.locations.model import Location

class Review(abstract.Model):
    rating = IntegerField(validators=[MinValueValidator(0), MaxValueValidator(5)])
    review = TextField()

    user = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name='reviews'
    )
    location = ForeignKey(
        Location,
        on_delete=CASCADE,
        related_name='reviews'
    )

    objects = Manager()

    class Meta(abstract.Model.Meta):
        unique_together = ('user', 'location')
        get_latest_by = 'created'
