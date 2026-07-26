from __future__ import annotations

import logging
import math

from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.serializer import PinSerializer
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, delete_pin, move_pin_to_coordinates

logger = logging.getLogger(__name__)


class PinViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """PATCH/DELETE only - see the "deliberately minimal" note in dashboard/urls.py.

    Only ``mixins.DestroyModelMixin`` is mixed in, and ``update`` is never
    defined (only ``partial_update``), so the router never binds GET, PUT, or
    POST/list at all - creating a pin goes through ``MapController.post_add_pin``
    instead, matching the map's own add-pin flow.
    """

    serializer_class = PinSerializer
    basename = "pins"
    lookup_field = "uuid"

    def get_queryset(self):
        if not self.request:
            return Pin.objects.none()
        return Pin.objects.select_related("location").filter(profile__user=self.request.user)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        logger.info("Update request initiated by user %s", request.user.id)
        if instance.profile.user != request.user:
            logger.error(
                "User %s attempted to update pin %s, but does not have permission",
                request.user.id,
                instance.id,
            )
            return Response(status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            if "latitude" in request.data or "longitude" in request.data:
                error = self._apply_coordinates(instance, request.data)
                if error is not None:
                    return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

            serializer = self.get_serializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)
        logger.info("Pin with id %s updated", instance.id)
        return Response(serializer.data)

    @staticmethod
    def _apply_coordinates(instance: Pin, data) -> str | None:
        """Validate client-submitted lat/lng, then move *instance* via ``services.pin_edit``.

        Args:
            instance: The pin being moved.
            data: The request body, expected to carry ``latitude``/``longitude``.

        Returns:
            An error message if the coordinates are missing/invalid, else
            None once the move has been applied and saved.
        """
        try:
            latitude = float(data["latitude"])
            longitude = float(data["longitude"])
        except (KeyError, TypeError, ValueError):
            return "latitude and longitude must be numeric."
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            return "latitude and longitude must be finite numbers."
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return "latitude must be between -90 and 90, longitude between -180 and 180."

        move_pin_to_coordinates(instance, latitude, longitude)
        return None

    def perform_update(self, serializer):
        serializer.save(profile=self.request.user.profile)

    def destroy(self, request, *args, **kwargs):
        """Delete a pin, asking the client what to do with its child pins first.

        A pin with descendants requires an explicit ``children`` query param:
        without one the request is refused with 409 and a payload describing
        how many child pins exist, so the UI can ask the user. ``children=delete``
        removes the whole subtree (all of it restorable from Undo History);
        ``children=keep`` promotes the direct children to the deleted pin's own
        parent (or to top-level pins) and deletes only the pin itself.
        """
        logger.info("Delete request initiated by user %s", request.user.id)
        instance = self.get_object()
        if instance.profile.user != request.user:
            logger.error(
                "User %s attempted to delete pin %s, but does not have permission",
                request.user.id,
                instance.id,
            )
            return Response(status=status.HTTP_403_FORBIDDEN)

        children_mode = (request.query_params.get("children") or "").strip().lower()
        try:
            delete_pin(instance, children_mode=children_mode)
        except PinHasChildrenError as exc:
            return Response(
                {"requires_children_decision": True, "children": exc.descendant_count},
                status=status.HTTP_409_CONFLICT,
            )
        logger.info("Pin with id %s deleted", instance.id)
        return Response(status=status.HTTP_204_NO_CONTENT)
