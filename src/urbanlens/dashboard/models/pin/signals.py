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

from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.pin import Pin

logger = logging.getLogger(__name__)


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
    except Exception:
        # TODO: Consider more specific exception handling if possible
        logger.warning("suggest_category failed for pin %s", instance.pk, exc_info=True)
