from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.location import Location


@receiver(post_save, sender=Location, dispatch_uid="location_suggest_categories")
def suggest_and_add_categories(sender: type[Location], instance: Location, created: bool, **kwargs) -> None:
    """
    Suggests categories for a newly created Location instance and adds them.

    Args:
        sender (Model class): The model class.
        instance (Location): The actual instance being saved.
        created (bool): True if a new record was created.
        **kwargs: Additional keyword arguments.

    """
    if created:
        # Perform the category suggestion and addition only for new instances.
        # M2M changes from add_category(save=False) are committed by .add() directly;
        # no save() needed here.
        instance.suggest_category(append_suggestion=True)
