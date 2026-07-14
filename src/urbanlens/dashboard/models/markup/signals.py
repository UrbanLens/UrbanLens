"""Keep MarkupMap.inferred_pins in sync with a map's current geometry."""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup


def _defer_sync(markup_map_id: int) -> None:
    """Schedule a pin-inference resync for ``markup_map_id`` once the current transaction commits.

    Re-fetches the map at commit time (rather than reusing the signal's
    ``instance``) so the resync always sees the final, fully-saved state -
    important since ``MarkupMap.replace_items_from_snapshot`` triggers this
    once per item it recreates.

    Args:
        markup_map_id: Primary key of the map to resync.
    """

    def _run() -> None:
        from urbanlens.dashboard.services.map_pin_share_detection import sync_pin_inferences

        try:
            markup_map = MarkupMap.objects.get(pk=markup_map_id)
        except MarkupMap.DoesNotExist:
            return
        sync_pin_inferences(markup_map)

    transaction.on_commit(_run)


@receiver(post_save, sender=MarkupMap, dispatch_uid="markup_map_sync_pin_inferences_on_save")
def sync_pin_inferences_on_map_save(sender: type[MarkupMap], instance: MarkupMap, **kwargs) -> None:
    """Resync detected pins whenever a map's viewport is created or updated."""
    _defer_sync(instance.pk)


@receiver(post_save, sender=PinMarkup, dispatch_uid="pin_markup_sync_pin_inferences_on_save")
def sync_pin_inferences_on_item_save(sender: type[PinMarkup], instance: PinMarkup, **kwargs) -> None:
    """Resync the parent map's detected pins whenever a map-scoped markup item is added or edited."""
    if instance.parent_map_id:
        _defer_sync(instance.parent_map_id)


@receiver(post_delete, sender=PinMarkup, dispatch_uid="pin_markup_sync_pin_inferences_on_delete")
def sync_pin_inferences_on_item_delete(sender: type[PinMarkup], instance: PinMarkup, **kwargs) -> None:
    """Resync the parent map's detected pins whenever a map-scoped markup item is removed."""
    if instance.parent_map_id:
        _defer_sync(instance.parent_map_id)
