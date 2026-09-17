from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.wiki import Wiki


@receiver(post_save, sender=Wiki, dispatch_uid="wiki_suggest_categories")
def suggest_and_add_categories(sender: type[Wiki], instance: Wiki, created: bool, **kwargs) -> None:
    """Suggest categories for a newly created Wiki and attach them.
    Auto-tagging is deferred to Wiki creation (rather than Location creation) so it only runs for places that actually have a community page, keeping the label suggestion work bounded.

    Args:
        sender: The model class.
        instance: The actual instance being saved.
        created: True if a new record was created.
        **kwargs: Additional keyword arguments.
    """
    if not created:
        return
    from urbanlens.dashboard.services.core.celery import follow_on_queue

    queue = follow_on_queue()

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.bulk_followup import enqueue_follow_on
        from urbanlens.dashboard.tasks import suggest_wiki_categories, suggest_wiki_category

        enqueue_follow_on(suggest_wiki_category, suggest_wiki_categories, instance.pk, queue=queue)

    transaction.on_commit(_enqueue)
