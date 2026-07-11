from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.wiki import Wiki


@receiver(post_save, sender=Wiki, dispatch_uid="wiki_suggest_categories")
def suggest_and_add_categories(sender: type[Wiki], instance: Wiki, created: bool, **kwargs) -> None:
    """Suggest categories for a newly created Wiki and attach them.

    Auto-tagging is deferred to Wiki creation (rather than Location creation) so
    it only runs for places that actually have a community page, keeping the
    badge suggestion work bounded.

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
        from urbanlens.dashboard.tasks import suggest_wiki_category

        safely_enqueue_task(suggest_wiki_category, instance.pk)

    transaction.on_commit(_enqueue)
