from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.links.model import PinLink, WikiLink


@receiver(post_save, sender=PinLink, dispatch_uid="pin_link_archive_wayback")
def archive_pin_link(sender: type[PinLink], instance: PinLink, created: bool, **kwargs) -> None:
    """Queue a best-effort Wayback Machine archive for a newly added pin link."""
    if not created or instance.wayback_url:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import archive_link_to_wayback

        safely_enqueue_task(archive_link_to_wayback, "PinLink", instance.pk)

    transaction.on_commit(_enqueue)


@receiver(post_save, sender=WikiLink, dispatch_uid="wiki_link_archive_wayback")
def archive_wiki_link(sender: type[WikiLink], instance: WikiLink, created: bool, **kwargs) -> None:
    """Queue a best-effort Wayback Machine archive for a newly added wiki link."""
    if not created or instance.wayback_url:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import archive_link_to_wayback

        safely_enqueue_task(archive_link_to_wayback, "WikiLink", instance.pk)

    transaction.on_commit(_enqueue)
