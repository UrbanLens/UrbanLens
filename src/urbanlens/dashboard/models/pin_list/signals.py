import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.pin import Pin

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Pin, dispatch_uid="pin_smart_list_membership_sync")
def sync_smart_list_membership(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Keep smart PinList membership current whenever a pin is created/edited."""
    if not instance.profile_id:
        return

    def _run() -> None:
        from urbanlens.dashboard.services.pin_list_membership import sync_pin_against_smart_lists

        sync_pin_against_smart_lists(instance)

    transaction.on_commit(_run)
