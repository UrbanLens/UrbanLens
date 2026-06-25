import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.pin import Pin

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Pin, dispatch_uid="pin_invalidate_map_center")
def invalidate_profile_map_center(sender: type[Pin], instance: Pin, created: bool, **kwargs) -> None:
    """Clear the cached map center so it is recomputed on the next map load.

    Args:
        sender: The Pin model class.
        instance: The Pin that was just saved.
        created: True when a new pin row was inserted.
        **kwargs: Additional signal arguments.
    """
    if not created or not instance.profile_id:
        return
    from urbanlens.dashboard.models.profile.model import Profile

    Profile.objects.filter(pk=instance.profile_id).update(
        map_center_latitude=None,
        map_center_longitude=None,
    )
