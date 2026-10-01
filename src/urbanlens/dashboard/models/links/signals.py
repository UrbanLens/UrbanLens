from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.links.model import PinLink, WikiLink


@receiver(post_save, sender=PinLink, dispatch_uid="pin_link_archive_wayback")
def archive_pin_link(sender: type[PinLink], instance: PinLink, created: bool, **kwargs) -> None:
    """Queue a best-effort Wayback Machine archive for a newly added pin link.

    A confirmed import can create many PinLinks at once (one per link extracted from each
    pin's description) - ``enqueue_follow_on`` coalesces those into bounded chunks inside a
    bulk task's ``batching_follow_on_work()`` collector, same as it does for wiki creation,
    category suggestion, and reputation scoring (P109)."""
    if not created or instance.wayback_url:
        return
    from urbanlens.dashboard.services.core.celery import follow_on_queue

    queue = follow_on_queue()

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.bulk_followup import enqueue_follow_on
        from urbanlens.dashboard.tasks import archive_pin_link_to_wayback, archive_pin_links_to_wayback

        enqueue_follow_on(archive_pin_link_to_wayback, archive_pin_links_to_wayback, instance.pk, queue=queue)

    transaction.on_commit(_enqueue)


@receiver(post_save, sender=WikiLink, dispatch_uid="wiki_link_archive_wayback")
def archive_wiki_link(sender: type[WikiLink], instance: WikiLink, created: bool, **kwargs) -> None:
    """Queue a best-effort Wayback Machine archive for a newly added wiki link."""
    if not created or instance.wayback_url:
        return
    from urbanlens.dashboard.services.core.celery import follow_on_queue

    queue = follow_on_queue()

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.bulk_followup import enqueue_follow_on
        from urbanlens.dashboard.tasks import archive_wiki_link_to_wayback, archive_wiki_links_to_wayback

        enqueue_follow_on(archive_wiki_link_to_wayback, archive_wiki_links_to_wayback, instance.pk, queue=queue)

    transaction.on_commit(_enqueue)
