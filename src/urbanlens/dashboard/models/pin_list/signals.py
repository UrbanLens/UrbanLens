import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

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
