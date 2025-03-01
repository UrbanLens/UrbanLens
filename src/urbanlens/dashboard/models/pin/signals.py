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
from django.db.models.signals import post_save
from django.dispatch import receiver
from UrbanLens.dashboard.models.pin import Pin

@receiver(post_save, sender=Pin)
def suggest_and_add_categories(sender, instance : Pin, created, **kwargs):
    """
    Suggests categories for a newly created Pin instance and adds them.

    Args:
        sender (Model class): The model class.
        instance (Pin): The actual instance being saved.
        created (bool): True if a new record was created.
        **kwargs: Additional keyword arguments.
    """
    if created: 
        # Perform the category suggestion and addition only for new instances
        instance.suggest_category(append_suggestion=True)
        instance.save()
