from __future__ import annotations

import logging

from rest_framework import status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.serializer import PinSerializer
from urbanlens.dashboard.services.undo.service import stash_for_undo

logger = logging.getLogger(__name__)


class PinViewSet(viewsets.ModelViewSet):
    serializer_class = PinSerializer
    basename = "pins"
    lookup_field = "uuid"

    def get_queryset(self):
        if not self.request:
            return Pin.objects.none()
        return Pin.objects.select_related("location").filter(profile__user=self.request.user)

    def create(self, request, *args, **kwargs):
        logger.info("Create request initiated by user %s", request.user.id)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        latitude = serializer.validated_data.get("latitude")
        longitude = serializer.validated_data.get("longitude")
        nearby_pins = Pin.objects.nearby_pins(latitude, longitude, radius=0.1)
        if nearby_pins.exists():
            return Response(
                {"detail": "A pin already exists within a small radius."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        logger.info("Pin created with id %s", serializer.data["id"])
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save(profile=self.request.user.profile)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        logger.info("Update request initiated by user %s", request.user.id)
        if instance.profile.user != request.user:
            logger.error(
                "User %s attempted to update pin %s, but does not have permission",
                request.user.id,
                instance.id,
            )
            return Response(status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info("Pin with id %s updated", instance.id)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        logger.info("Delete request initiated by user %s", request.user.id)
        instance = self.get_object()
        if instance.profile.user != request.user:
            logger.error(
                "User %s attempted to delete pin %s, but does not have permission",
                request.user.id,
                instance.id,
            )
            return Response(status=status.HTTP_403_FORBIDDEN)
        subtree = list(Pin.objects.filter(pk=instance.pk).with_descendants())
        stash_for_undo("pin", subtree, instance.profile)
        for descendant in subtree:
            descendant.delete()
        logger.info("Pin with id %s deleted", instance.id)
        return Response(status=status.HTTP_204_NO_CONTENT)
