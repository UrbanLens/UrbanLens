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
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
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
from djangofoundry.models.fields import ForeignKey, IntegerField, TextField
from dashboard.models import abstract
from dashboard.models.locations.model import Location

class Review(abstract.Model):
    user = ForeignKey(User, on_delete=CASCADE)
    location = ForeignKey(Location, on_delete=CASCADE)
    rating = IntegerField(validators=[MinValueValidator(0), MaxValueValidator(5)])
    review = TextField()

    class Meta(abstract.Model.Meta):
        unique_together = ('user', 'location')
