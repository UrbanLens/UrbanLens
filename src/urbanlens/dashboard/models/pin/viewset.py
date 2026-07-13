from __future__ import annotations

import logging

from django.db import transaction
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
