from __future__ import annotations

import logging
import math

from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.serializer import PinSerializer
from urbanlens.dashboard.services.pin_edit import PinHasChildrenError, PinMoveError, delete_pin, move_pin_to_coordinates
from urbanlens.dashboard.services.wiki_access import wikis_hidden_by_pin_move

logger = logging.getLogger(__name__)

#: Values a client may send for ``confirm_wiki_loss`` to mean "yes, go ahead".
#: JSON bodies send a real boolean; form-encoded ones can only send a string.
_TRUTHY = {"1", "true", "yes", "on"}


def _wiki_loss_confirmed(data) -> bool:
    """Whether the client has already acknowledged losing wiki access.

    Args:
        data: The request body.

    Returns:
        Whether ``confirm_wiki_loss`` was sent as a truthy value.
    """
    value = data.get("confirm_wiki_loss")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUTHY


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
            # Everything is validated before anything is asked or written, so a
            # confirmed move can't then be rejected for an unrelated bad field.
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)

            if "latitude" in request.data or "longitude" in request.data:
                parsed = self._parse_coordinates(request.data)
                if isinstance(parsed, str):
                    return Response({"detail": parsed}, status=status.HTTP_400_BAD_REQUEST)
                latitude, longitude = parsed

                # Moving a pin off a place's grounds silently drops the owner's
                # access to that place's community wiki (visibility is derived
                # from where their pins are, not stored). Refuse once with 409
                # and say which wikis are at stake, so the UI can ask rather
                # than let it happen invisibly; the client re-sends with
                # confirm_wiki_loss to go ahead.
                if not _wiki_loss_confirmed(request.data):
                    lost = wikis_hidden_by_pin_move(instance, latitude, longitude)
                    if lost:
                        return Response(
                            {
                                "requires_wiki_loss_confirmation": True,
                                "wikis": [{"name": wiki.name, "slug": wiki.location.slug} for wiki in lost],
                            },
                            status=status.HTTP_409_CONFLICT,
                        )

                try:
                    move_pin_to_coordinates(instance, latitude, longitude)
                except PinMoveError as exc:
                    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            self.perform_update(serializer)
        logger.info("Pin with id %s updated", instance.id)
        return Response(serializer.data)

    @staticmethod
    def _parse_coordinates(data) -> tuple[float, float] | str:
        """Validate client-submitted lat/lng.

        Args:
            data: The request body, expected to carry ``latitude``/``longitude``.

        Returns:
            The parsed (latitude, longitude), or an error message string when
            they are missing or invalid.
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
        return latitude, longitude

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
