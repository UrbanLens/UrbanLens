"""Publish a tallied service's limits the moment its row is saved, so no call has to read the row itself."""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit


@receiver(post_save, sender=ApiRateLimit, dispatch_uid="api_rate_limit_publish_tallied_limits")
def publish_tallied_limits(sender: type[ApiRateLimit], instance: ApiRateLimit, **kwargs: object) -> None:
    """Share the saved limits with every process once the save commits (``services.core.call_tally``)."""
    from urbanlens.dashboard.services.core.rate_limiter import CallLedger, call_ledger

    if call_ledger(instance.service) is not CallLedger.TALLIED:
        return

    def publish() -> None:
        from urbanlens.dashboard.services.core.call_tally import publish_limits

        publish_limits(instance.service, instance)

    transaction.on_commit(publish)
