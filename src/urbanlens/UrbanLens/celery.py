"""Celery application for UrbanLens background work."""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.signals import task_failure, task_retry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

logger = logging.getLogger(__name__)

app = Celery("urbanlens")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(task_track_started=True)
app.autodiscover_tasks()


@task_failure.connect
def log_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **_extra) -> None:  # noqa: PLR0917
    """Log Celery task failures with enough context for operations debugging."""
    logger.error(
        "Celery task failed: task=%s id=%s args=%s kwargs=%s exception=%s",
        getattr(sender, "name", sender),
        task_id,
        args,
        kwargs,
        exception,
        exc_info=einfo.exc_info if einfo else None,
    )


@task_retry.connect
def log_task_retry(request=None, reason=None, einfo=None, **_extra) -> None:
    """Log Celery retries separately from final task failures."""
    logger.warning(
        "Celery task retrying: task=%s id=%s reason=%s",
        getattr(request, "task", None),
        getattr(request, "id", None),
        reason,
        exc_info=einfo.exc_info if einfo else None,
    )
