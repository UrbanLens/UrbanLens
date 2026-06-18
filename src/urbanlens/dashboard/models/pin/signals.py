"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    signals.py                                                                                         *
*        - Path:    /dashboard/models/pin/signals.py                                                             *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-03-22                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

import logging
import os

from django.db.models.signals import post_save
from django.dispatch import receiver
import requests

from urbanlens.dashboard.models.pin import Pin

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Pin, dispatch_uid="pin_invalidate_map_center")
def invalidate_profile_map_center(sender, instance: Pin, created: bool, **kwargs) -> None:
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


@receiver(post_save, sender=Pin, dispatch_uid="pin_suggest_categories")
def suggest_and_add_categories(sender, instance: Pin, created, **kwargs):
    """
    Suggests categories for a newly created Pin instance and adds them.

    Args:
        sender (Model class): The model class.
        instance (Pin): The actual instance being saved.
        created (bool): True if a new record was created.
        **kwargs: Additional keyword arguments.

    """
    if not created:
        return
    
    # Perform the category suggestion and addition only for new instances.
    # M2M changes from add_category(save=False) are committed by .add() directly;
    # no save() needed here.
    try:
        instance.suggest_category(append_suggestion=True)
    except (requests.RequestException, ValueError):
        logger.warning("suggest_category failed for pin %s", instance.pk, exc_info=True)
