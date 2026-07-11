from __future__ import annotations

import logging
import math

from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.serializer import PinSerializer
from urbanlens.dashboard.services.undo.service import stash_for_undo

logger = logging.getLogger(__name__)


class PinViewSet(
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Minimal REST access to the requesting user's own pins.

    The frontend only uses two operations here - PATCH (map popup quick-edit
    and pin dragging) and DELETE (pin delete with undo stash) - so nothing
    else is exposed. Pin creation goes through the map's HTMX flow
    (``MapController.post_add_pin``), and listing/detail data comes from the
    map JSON endpoints. The queryset is always scoped to the authenticated
    user, so detail routes 404 for pins belonging to anyone else.
    """

    serializer_class = PinSerializer
    basename = "pins"
    lookup_field = "uuid"
    # PATCH and DELETE only: PUT is unused by the app, so it is not exposed.
    http_method_names = ["patch", "delete", "head", "options"]

    def get_queryset(self):
        """Return the requesting user's pins with their locations prefetched.

        Returns:
            QuerySet of pins owned by ``request.user``, or an empty queryset
            when there is no request (e.g. schema generation).
        """
        if not self.request or not self.request.user.is_authenticated:
            return Pin.objects.none()
        return Pin.objects.select_related("location").filter(profile__user=self.request.user)

    def update(self, request, *args, **kwargs):
        """Partially update one of the requesting user's pins.

        ``latitude``/``longitude`` in the body move the pin: coordinates live
        on the shared Location (whose coordinates are immutable), so a move
        repoints the pin at a find-or-created Location at the new point -
        the same semantics as ``MapController.patch_pin``. They are read-only
        on the serializer precisely because they are not Pin columns.

        Returns:
            200 with the serialized pin, or 400 for unparseable coordinates.
            Pins owned by other users 404 via the scoped queryset.
        """
        instance = self.get_object()
        logger.info("Update request initiated by user %s", request.user.id)
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        latitude = request.data.get("latitude")
        longitude = request.data.get("longitude")
        save_kwargs = {}
        if latitude is not None and longitude is not None:
            from urbanlens.dashboard.models.location.model import Location

            try:
                lat_f = float(latitude)
                lng_f = float(longitude)
            except (TypeError, ValueError):
                lat_f = lng_f = math.nan
            if not (math.isfinite(lat_f) and math.isfinite(lng_f) and -90 <= lat_f <= 90 and -180 <= lng_f <= 180):
                return Response(
                    {"detail": "Valid 'latitude' and 'longitude' are required to move a pin."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            location, _ = Location.objects.get_nearby_or_create(lat_f, lng_f)
            save_kwargs["location"] = location

        serializer.save(**save_kwargs)
        logger.info("Pin with id %s updated", instance.id)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        """Delete a pin and its detail-pin subtree, stashing them for undo.

        Returns:
            204 on success. Pins owned by other users 404 via the scoped
            queryset.
        """
        logger.info("Delete request initiated by user %s", request.user.id)
        instance = self.get_object()
        subtree = list(Pin.objects.filter(pk=instance.pk).with_descendants())
        stash_for_undo("pin", subtree, instance.profile)
        for descendant in subtree:
            descendant.delete()
        logger.info("Pin with id %s deleted", instance.id)
        return Response(status=status.HTTP_204_NO_CONTENT)
