"""Floorplan model signals."""

from __future__ import annotations

from django.db.models.signals import post_delete
from django.dispatch import receiver

from urbanlens.dashboard.models.floorplans.model import FloorplanMarker


@receiver(post_delete, sender=FloorplanMarker, dispatch_uid="floorplan_marker_delete_linked_pin")
def delete_linked_pin(sender: type[FloorplanMarker], instance: FloorplanMarker, **kwargs) -> None:
    """Remove a marker's detail-pin twin however the marker itself went away.

    ``linked_pin`` is ``CASCADE`` in the pin -> marker direction (deleting the
    detail pin already removes the marker with it), but nothing expresses the
    reverse: a marker can also vanish because its floor, or the whole
    floorplan version, was deleted - a plain FK cascade Django's own
    collector runs directly in SQL, never touching
    ``services.floorplans.serialization``'s per-marker sync code. This is the
    one place that reliably sees every deletion path, since Django's
    collector fetches instances (rather than fast-deleting in bulk) whenever
    a model has a connected signal receiver.

    A filtered queryset delete rather than ``instance.linked_pin.delete()``:
    when the *pin* side is what started the cascade (a detail pin deleted
    from the pin page), this fires after the pin's own row is already gone,
    and fetching it via the relation would raise ``DoesNotExist``. Deleting
    a queryset that already matches nothing is a harmless no-op instead.
    """
    if instance.linked_pin_id:
        from urbanlens.dashboard.models.pin.model import Pin

        Pin.objects.filter(pk=instance.linked_pin_id).delete()
