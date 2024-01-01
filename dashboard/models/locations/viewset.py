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
*        Path:    /dashboard/models/locations/viewset.py                                                               *
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
import logging
from rest_framework import viewsets, status
from rest_framework.response import Response
from dashboard.models.locations.model import Location, LocationStatus
from dashboard.models.locations.serializer import LocationSerializer

logger = logging.getLogger(__name__)

class LocationViewSet(viewsets.ModelViewSet):
    serializer_class = LocationSerializer
    basename = 'locations'

    def perform_update(self, serializer):
        serializer.save()

    def get_queryset(self):
        if not self.request:
            return Location.objects.none()
        return Location.objects.filter(profile__user=self.request.user)

    def create(self, request, *args, **kwargs):
        logger.info(f"Create request initiated by user {request.user.id}")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        latitude = serializer.validated_data.get('latitude')
        longitude = serializer.validated_data.get('longitude')
        nearby_locations = Location.objects.nearby_locations(latitude, longitude, radius=0.1)
        if nearby_locations.exists():
            return Response({"detail": "A location already exists within a small radius."}, status=status.HTTP_400_BAD_REQUEST)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        logger.info(f"Location created with id {serializer.data['id']}")
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, profile=self.request.user.profile, status=self.request.data.get('status', LocationStatus.NOT_VISITED))

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        logger.info(f"Update request initiated by user {request.user.id}")
        if instance.profile.user != request.user:
            logger.error(f"User %s attempted to update location %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info(f"Location with id {instance.id} updated")
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        logger.info(f"Delete request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.profile.user != request.user:
            logger.error(f"User %s attempted to delete location %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        logger.info(f"Location with id {instance.id} deleted")
        return super().destroy(request, *args, **kwargs)
