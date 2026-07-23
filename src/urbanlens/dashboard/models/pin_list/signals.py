import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin import Pin

logger = logging.getLogger(__name__)


def _resync_pin_against_smart_lists(instance: Pin) -> None:
    if not instance.profile_id:
        return

    def _run() -> None:
        from urbanlens.dashboard.services.pin_list_membership import sync_pin_against_smart_lists

        sync_pin_against_smart_lists(instance)

    transaction.on_commit(_run)


@receiver(post_save, sender=Pin, dispatch_uid="pin_smart_list_membership_sync")
def sync_smart_list_membership(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Keep smart PinList membership current whenever a pin is created/edited."""
    _resync_pin_against_smart_lists(instance)


@receiver(m2m_changed, sender=Pin.labels.through, dispatch_uid="pin_labels_smart_list_membership_sync")
def sync_smart_list_membership_for_labels(sender, instance: Pin, action: str, reverse: bool = False, **kwargs) -> None:
    """Keep smart PinList membership current whenever a pin's labels change.

    Label add/remove/clear never calls ``Pin.save()``, so this can't rely on
    the ``post_save`` receiver above - a smart list whose ``smart_filter``
    includes ``tags``/``exclude_tags``/``label_groups`` would otherwise never
    pick up a pin gaining or losing a matching label.
    """
    if reverse or action not in {"post_add", "post_remove", "post_clear"}:
        return
    _resync_pin_against_smart_lists(instance)


def _touch_pin(pin_id: int | None) -> None:
    """Bump a pin's ``updated`` timestamp via a full save, on commit.

    Going through ``Pin.save(update_fields=["updated"])`` (rather than a bare
    ``.update()``) deliberately re-fires ``sync_smart_list_membership`` above
    for free, and also keeps ``services.saved_filter_cache``'s
    ``Max(Pin.updated)`` cache-key fingerprint current - both would otherwise
    miss changes that don't call ``Pin.save()`` themselves (a PinLink
    add/remove, or a child pin being created/deleted under a parent).
    """
    if not pin_id:
        return

    def _run() -> None:
        pin = Pin.objects.filter(pk=pin_id).first()
        if pin is not None:
            pin.save(update_fields=["updated"])

    transaction.on_commit(_run)


@receiver(post_save, sender=PinLink, dispatch_uid="pin_link_smart_list_resync_on_save")
def resync_pin_on_link_saved(sender: type[PinLink], instance: PinLink, **kwargs) -> None:
    """A link add (or edit) can flip a pin's "has links" filter/smart-list match.

    ``PinLink`` writes never call ``Pin.save()``, so nothing else notices.
    """
    _touch_pin(instance.pin_id)


@receiver(post_delete, sender=PinLink, dispatch_uid="pin_link_smart_list_resync_on_delete")
def resync_pin_on_link_deleted(sender: type[PinLink], instance: PinLink, **kwargs) -> None:
    """Mirrors ``resync_pin_on_link_saved`` for the "no links left" case."""
    _touch_pin(instance.pin_id)


@receiver(post_save, sender=Pin, dispatch_uid="detail_pin_parent_smart_list_resync_on_create")
def resync_parent_on_detail_pin_created(sender: type[Pin], instance: Pin, created: bool, **kwargs) -> None:
    """A new detail pin changes its parent's "detail pin count" filter/smart-list match.

    Only fires on creation (not every edit of an existing detail pin) - the
    count only changes when a detail pin is added or removed.
    """
    if not created:
        return
    _touch_pin(instance.parent_pin_id)


@receiver(post_delete, sender=Pin, dispatch_uid="detail_pin_parent_smart_list_resync_on_delete")
def resync_parent_on_detail_pin_deleted(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Mirrors ``resync_parent_on_detail_pin_created`` for detail-pin deletion."""
    _touch_pin(instance.parent_pin_id)
