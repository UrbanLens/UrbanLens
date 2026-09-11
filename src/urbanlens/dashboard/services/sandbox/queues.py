"""The Celery queues this deployment runs, and which container drains each one.
Celery reads ``Task.queue`` through ``_get_exec_options``, so one declaration next to the task body routes every caller - including callers written later, which is the failure mode a per-call-site ``queue=`` argument invites: one missed call and an untrusted parse silently runs on the unrestricted worker."""

from __future__ import annotations

from enum import StrEnum


class Queue(StrEnum):
    """A Celery queue name.

    Attributes:
        DEFAULT: Celery's own default queue name. Anything without an explicit
            queue lands here, drained by ``celery-worker``.
        PANEL_FETCH: External-data panel fetches for the Private Pin page.
        SANDBOX: Interactive parsing of untrusted user-supplied bytes - image
            decode, video transcode, document conversion. Drained by
            ``media-worker``, which has no route to the internet.
        SANDBOX_BATCH: Long-running untrusted-parse batch jobs - archive walks,
            data imports. Same isolation as :attr:`SANDBOX`, its own worker, so
            an hour-long import never queues in front of a photo upload.
        AI: The assistant's tool loop, drained by ``ai-worker``. No REData,
            OAuth or provider credentials in that container - model calls go
            out over HTTP to ``ai-inference``. See ``ai_queue()``.
    """

    DEFAULT = "celery"
    PANEL_FETCH = "panel_fetch"
    SANDBOX = "sandbox"
    SANDBOX_BATCH = "sandbox_batch"
    AI = "ai"


def sandbox_queue(*, batch: bool = False) -> str:
    """The queue untrusted-parse tasks should be routed to.
    Read once per task definition, at import time, so it appears in the task's own exec options rather than at each call site.

    Args:
        batch: True for a task that runs for minutes rather than for a moment -
            it goes to :attr:`Queue.SANDBOX_BATCH` so it cannot occupy the
            interactive pool. Same isolation either way.

    Returns:
        The matching sandbox queue when a sandbox worker is deployed, else
        :attr:`Queue.DEFAULT` - an install with no ``media-worker`` container
        keeps processing uploads on the ordinary worker instead of enqueuing
        into a queue that nothing drains."""
    from django.conf import settings

    if not getattr(settings, "UL_SANDBOX_ENABLED", False):
        return Queue.DEFAULT
    return Queue.SANDBOX_BATCH if batch else Queue.SANDBOX


def ai_queue() -> str:
    """The queue the assistant's tool-loop task should be routed to.
    Read once per task definition, at import time, so it appears in the task's own exec options rather than at each call site - matching :func:`sandbox_queue`.

    Returns:
        :attr:`Queue.AI`, always."""
    return Queue.AI
