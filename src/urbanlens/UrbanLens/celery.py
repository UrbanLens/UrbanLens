"""Celery application for UrbanLens background work."""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger, task_failure, task_prerun, task_retry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

logger = logging.getLogger(__name__)

# The base task class turns a soft time limit into an error broad `except Exception` handlers cannot swallow.
app = Celery("urbanlens", task_cls="urbanlens.dashboard.services.core.task_limits:UrbanLensTask")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(task_track_started=True)
app.autodiscover_tasks()


@after_setup_logger.connect
@after_setup_task_logger.connect
def redact_worker_log_handlers(**_extra) -> None:
    """Give the handlers Celery installs in a worker (it replaces the root logger's) the redaction ``LOGGING``'s handlers have."""
    from urbanlens.UrbanLens.logging_filters import redact_every_handler

    redact_every_handler()


@task_failure.connect
def log_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **_extra) -> None:  # noqa: PLR0917 - a signal receiver: its parameters are the documented task_failure signal arguments, which Celery passes by keyword
    """Log Celery task failures, with the task's arguments redacted."""
    from urbanlens.dashboard.services.security.redact import redact_call_arguments

    logger.error(
        "Celery task failed: task=%s id=%s arguments=%s exception=%s",
        getattr(sender, "name", sender),
        task_id,
        redact_call_arguments(getattr(sender, "run", None), args or (), kwargs or {}),
        exception,
        exc_info=einfo.exc_info if einfo else None,
    )


@task_retry.connect
def log_task_retry(request=None, reason=None, einfo=None, **_extra) -> None:
    """Log Celery retries."""
    logger.warning(
        "Celery task retrying: task=%s id=%s reason=%s",
        getattr(request, "task", None),
        getattr(request, "id", None),
        reason,
        exc_info=einfo.exc_info if einfo else None,
    )


@task_prerun.connect
def bind_write_source(task_id=None, task=None, **_extra) -> None:
    """Mark writes inside a Celery task as automatic.

    Skipped in eager mode, which runs inline in the caller's context.
    """
    if getattr(getattr(task, "request", None), "is_eager", False):
        return

    from urbanlens.dashboard.models.abstract.versioning import WriteSource, bind_write_source

    bind_write_source(WriteSource.AUTOMATIC)
