"""Celery application for UrbanLens background work."""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.signals import task_failure, task_prerun, task_retry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

logger = logging.getLogger(__name__)

app = Celery("urbanlens")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(task_track_started=True)
app.autodiscover_tasks()


@task_failure.connect
def log_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **_extra) -> None:
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


@task_prerun.connect
def bind_write_source(task_id=None, task=None, **_extra) -> None:
    """Mark writes inside a Celery task as automatic.

    The counterpart to ``WriteSourceMiddleware``: field provenance is inferred
    from context rather than declared at each call site, and background work is
    where enrichment lives. A task acting *for* a person - one kicked off by a
    request that then does the user's edit - overrides this with
    ``writing_as`` at the point it knows.

    Skipped in eager mode, which is not a detail. ``task_always_eager`` runs the
    task inline in the caller's own context and Celery does not isolate it, so
    binding here would leave the *enclosing request* marked AUTOMATIC for the
    rest of its life - and AUTOMATIC is the source every concealed viewer sees.
    A request that enqueues anything would have its subsequent writes attributed
    to nobody. Eager mode is off in the deployed stacks and on wherever the
    suite runs it, which is exactly where the provenance tests live.

    Outside eager mode there is deliberately no ``task_postrun`` counterpart:
    each task run gets its own context, and a reused worker thread rebinds this
    before that task's first write.
    """
    if getattr(getattr(task, "request", None), "is_eager", False):
        return

    from urbanlens.dashboard.models.abstract.versioning import WriteSource, bind_write_source

    bind_write_source(WriteSource.AUTOMATIC)
