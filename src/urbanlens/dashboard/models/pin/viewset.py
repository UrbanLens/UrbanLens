from __future__ import annotations

import logging
import math

from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.serializer import PinSerializer
from urbanlens.dashboard.services.undo.service import stash_for_undo

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
        """Move *instance* to a new/existing Location per client-submitted lat/lng.

        Coordinates live on ``Location`` (not ``Pin``), so this repoints
        ``instance.location`` rather than writing through the serializer.

        Args:
            instance: The pin being moved.
            data: The request body, expected to carry ``latitude``/``longitude``.

        Returns:
            An error message if the coordinates are missing/invalid, else
            None once the move has been applied and saved.
        """
        from urbanlens.dashboard.models.location.model import Location

        try:
            latitude = float(data["latitude"])
            longitude = float(data["longitude"])
        except (KeyError, TypeError, ValueError):
            return "latitude and longitude must be numeric."
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            return "latitude and longitude must be finite numbers."
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return "latitude must be between -90 and 90, longitude between -180 and 180."

        location, _created = Location.objects.get_nearby_or_create(latitude, longitude)
        instance.location = location
        instance.save(update_fields=["location", "updated"])
        return None

    def perform_update(self, serializer):
        serializer.save(profile=self.request.user.profile)

    def destroy(self, request, *args, **kwargs):
        """Delete a pin, asking the client what to do with its sub pins first.

        A pin with descendants requires an explicit ``children`` query param:
        without one the request is refused with 409 and a payload describing
        how many sub pins exist, so the UI can ask the user. ``children=delete``
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
        subtree = list(Pin.objects.filter(pk=instance.pk).with_descendants())
        descendant_count = len(subtree) - 1

        if descendant_count and children_mode not in {"delete", "keep"}:
            return Response(
                {"requires_children_decision": True, "children": descendant_count},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            if descendant_count and children_mode == "keep":
                deferred_ids = self._promote_children(instance)
                subtree = [instance]
                stash_for_undo("pin", subtree, instance.profile)
                instance.delete()
                self._finish_deferred_promotions(instance.profile_id, deferred_ids)
            else:
                stash_for_undo("pin", subtree, instance.profile)
                for descendant in subtree:
                    descendant.delete()
        logger.info("Pin with id %s deleted", instance.id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _promote_children(instance: Pin) -> list[int]:
        """Re-parent *instance*'s direct children ahead of its deletion.

        Children move up to the deleted pin's own parent; when the deleted pin
        was top-level they become top-level pins themselves. A child whose
        Location already has another top-level pin nests under that pin instead
        (top-level pins are unique per Location+profile).

        Children that would collide with *instance*'s own root slot (same
        Location) can only become top-level once the pin is actually gone, so
        they are temporarily self-parented (detaching them from the doomed
        cascade) and returned for :meth:`_finish_deferred_promotions`.

        Args:
            instance: The pin about to be deleted.

        Returns:
            Primary keys of children whose promotion must finish post-delete.
        """
        new_parent_id = instance.parent_pin_id
        deferred_ids: list[int] = []
        for child in Pin.objects.filter(parent_pin=instance):
            if new_parent_id is not None:
                child.parent_pin_id = new_parent_id
                child.save(update_fields=["parent_pin", "updated"])
                continue
            other_root = Pin.objects.filter(profile_id=instance.profile_id, location_id=child.location_id, parent_pin__isnull=True).exclude(pk=instance.pk).first()
            if other_root is not None:
                child.parent_pin_id = other_root.pk
                child.save(update_fields=["parent_pin", "updated"])
            elif child.location_id == instance.location_id:
                # Bypass save() so no side effects run for this transient state.
                Pin.objects.filter(pk=child.pk).update(parent_pin_id=child.pk)
                deferred_ids.append(child.pk)
            else:
                child.parent_pin = None
                child.save(update_fields=["parent_pin", "updated"])
        return deferred_ids

    @staticmethod
    def _finish_deferred_promotions(profile_id: int, deferred_ids: list[int]) -> None:
        """Finish promoting the children held back by :meth:`_promote_children`.

        Runs after the parent pin's row is gone, so its root slot is free. If
        several deferred children share one Location, the first becomes the
        top-level pin and the rest nest under it.

        Args:
            profile_id: Owner of the pins (root uniqueness is per profile).
            deferred_ids: Primary keys of the temporarily self-parented children.
        """
        for child in Pin.objects.filter(pk__in=deferred_ids):
            existing_root = Pin.objects.filter(profile_id=profile_id, location_id=child.location_id, parent_pin__isnull=True).exclude(pk=child.pk).first()
            child.parent_pin_id = existing_root.pk if existing_root is not None else None
            child.save(update_fields=["parent_pin", "updated"])
