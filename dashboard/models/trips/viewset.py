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
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.response import Response
from dashboard.models.trips.model import Trip
from dashboard.models.trips.serializer import TripSerializer

logger = logging.getLogger(__name__)

class TripViewSet(viewsets.ModelViewSet):
    serializer_class = TripSerializer
    basename = 'trips'

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Trip.objects.none()
        return Trip.objects.filter(profiles__user=user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def remove_user(self, request, pk=None):
        trip = self.get_object()
        user = request.user
        if user in trip.profiles.all():
            trip.profiles.remove(user.profile)

            # If the trip is empty, delete it
            if trip.profiles.count() < 1:
                trip.delete()

            return Response({'status': 'user removed'})
        else:
            return Response({'status': 'user is not in trip'}, status=status.HTTP_400_BAD_REQUEST)
