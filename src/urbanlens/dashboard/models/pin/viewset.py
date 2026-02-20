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
*        Path:    /dashboard/models/pin/viewset.py                                                               *
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
import logging

from rest_framework import status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin, PinStatus
from urbanlens.dashboard.models.pin.serializer import PinSerializer

logger = logging.getLogger(__name__)


class PinViewSet(viewsets.ModelViewSet):
    serializer_class = PinSerializer
    basename = "pins"

    def get_queryset(self):
        if not self.request:
            return Pin.objects.none()
        return Pin.objects.filter(profile__user=self.request.user)

    def create(self, request, *args, **kwargs):
        logger.info(f"Create request initiated by user {request.user.id}")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        latitude = serializer.validated_data.get("latitude")
        longitude = serializer.validated_data.get("longitude")
        nearby_pins = Pin.objects.nearby_pins(latitude, longitude, radius=0.1)
        if nearby_pins.exists():
            return Response({"detail": "A pin already exists within a small radius."}, status=status.HTTP_400_BAD_REQUEST)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        logger.info(f"Pin created with id {serializer.data['id']}")
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, profile=self.request.user.profile, status=self.request.data.get("status", PinStatus.NOT_VISITED))

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        logger.info(f"Update request initiated by user {request.user.id}")
        if instance.profile.user != request.user:
            logger.error("User %s attempted to update pin %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info(f"Pin with id {instance.id} updated")
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        logger.info(f"Delete request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.profile.user != request.user:
            logger.error("User %s attempted to delete pin %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        logger.info(f"Pin with id {instance.id} deleted")
        return super().destroy(request, *args, **kwargs)
