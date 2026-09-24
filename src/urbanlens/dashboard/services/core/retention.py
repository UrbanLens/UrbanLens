"""Retention sweeps: delete rows older than the period ``SiteSettings`` keeps, a bounded batch at a time."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet

logger = logging.getLogger(__name__)

#: Most batches one run deletes, so a first run over years of rows cannot hold a worker for hours.
MAX_BATCHES_PER_RUN = 200


def delete_in_batches(queryset: QuerySet[Model], *, batch_size: int, max_batches: int = MAX_BATCHES_PER_RUN) -> int:
    """Delete ``queryset``'s rows in primary-key batches, each its own statement and transaction.

    Args:
        queryset: The rows to delete.
        batch_size: Rows per batch; keep it small for a row that cascades to many others.
        max_batches: Most batches this call deletes; the next run carries on.

    Returns:
        How many of ``queryset``'s own rows were deleted, not counting cascades.
    """
    model = queryset.model
    deleted = 0
    for _ in range(max_batches):
        pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:batch_size])
        if not pks:
            break
        model._base_manager.filter(pk__in=pks).delete()  # noqa: SLF001 - Django's documented unfiltered manager
        deleted += len(pks)
    return deleted


def retention_cutoff(days: int, *, now: datetime | None = None) -> datetime | None:
    """The moment before which rows kept for ``days`` are due, or None when ``days`` is 0 (keep forever).

    Args:
        days: The retention period from ``SiteSettings``.
        now: The current time; defaults to now.

    Returns:
        The cutoff, or None.
    """
    if days <= 0:
        return None
    return (now or timezone.now()) - timedelta(days=days)


def prune_read_notifications(*, now: datetime | None = None) -> int:
    """Delete read notifications older than ``SiteSettings.notification_retention_days``.

    Unread ones stay: nobody has seen them yet.

    Args:
        now: The current time; defaults to now.

    Returns:
        How many notifications were deleted.
    """
    from urbanlens.dashboard.models.notifications.meta import Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.site_settings import SiteSettings

    cutoff = retention_cutoff(SiteSettings.get_current().notification_retention_days, now=now)
    if cutoff is None:
        return 0
    return delete_in_batches(NotificationLog.objects.filter(status=Status.READ, created__lt=cutoff), batch_size=5000)


def prune_device_scan_uploads(*, now: datetime | None = None) -> int:
    """Delete device-scan uploads older than ``SiteSettings.device_scan_retention_days``, with their entries and readings.

    Markers already derived from them keep their stored position and counts; only a later recompute stops
    seeing the deleted observations, which clustering ignores past ``LOOKBACK_DAYS`` anyway.

    Args:
        now: The current time; defaults to now.

    Returns:
        How many uploads were deleted.
    """
    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload
    from urbanlens.dashboard.models.site_settings import SiteSettings

    cutoff = retention_cutoff(SiteSettings.get_current().device_scan_retention_days, now=now)
    if cutoff is None:
        return 0
    # An upload carries up to 200 entries of 500 readings each, so a batch is kept to a few uploads.
    return delete_in_batches(DeviceScanUpload.objects.filter(created__lt=cutoff), batch_size=20)
