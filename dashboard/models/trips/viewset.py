"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    viewset.py                                                                                           *
*        Path:    /dashboard/models/trips/viewset.py                                                                   *
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
import logging
from rest_framework import viewsets, status
from rest_framework.response import Response
from dashboard.models.trips.model import Trip
from dashboard.models.trips.serializer import TripSerializer

logger = logging.getLogger(__name__)

class TripViewSet(viewsets.ModelViewSet):
    serializer_class = TripSerializer
    basename = 'trips'

    def get_queryset(self):
        if not self.request:
            return Trip.objects.none()
        return Trip.objects.all()