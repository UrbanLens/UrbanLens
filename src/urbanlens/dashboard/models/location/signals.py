from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.location import Location


@receiver(post_save, sender=Location, dispatch_uid="location_suggest_categories")
def suggest_and_add_categories(sender: type[Location], instance: Location, created: bool, **kwargs) -> None:
    """Suggests categories for a newly created Location instance and adds them.

    Args:
        sender: The model class.
        instance: The actual instance being saved.
        created: True if a new record was created.
        **kwargs: Additional keyword arguments.
    """
    if not created:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import suggest_location_category

        safely_enqueue_task(suggest_location_category, instance.pk)

    transaction.on_commit(_enqueue)
