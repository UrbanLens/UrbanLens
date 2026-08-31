"""The Celery queues this deployment runs, and which container drains each one.

A queue here is not a scheduling nicety - it is the unit of *isolation*. Each
one is drained by its own container in ``docker-compose.yml``, and those
containers deliberately differ in what they can reach:

===================  =========================  ==================================
Queue                Container                  Reaches
===================  =========================  ==================================
``celery``           ``celery-worker``          everything (DB, broker, internet)
``panel_fetch``      ``celery-worker-panels``   everything - it *is* the API caller
``sandbox``          ``media-worker``           DB, broker, media volumes. No internet,
                                                no third-party API keys, no capabilities
``sandbox_batch``    ``media-worker-batch``     identical isolation to ``sandbox``
``ai_inference``     (not yet deployed)         reserved; see docs/MEDIA_PIPELINE.md
===================  =========================  ==================================

``sandbox`` and ``sandbox_batch`` differ in *scheduling*, not in trust: both are
drained inside the same isolated network with the same reduced environment. The
split exists because the two workloads have incompatible latency profiles - an
upload is interactive and sub-second, a data import walks a 500MB archive for up
to ``CELERY_TASK_TIME_LIMIT``. Sharing one small pool let the second starve the
first. Route by *duration*, not by risk.

The queue a task runs on is declared on the task itself
(``@shared_task(queue=...)``), not passed at each ``apply_async`` call site.
Celery reads ``Task.queue`` through ``_get_exec_options``, so one declaration
next to the task body routes every caller - including callers written later,
which is the failure mode a per-call-site ``queue=`` argument invites: one
missed call and an untrusted parse silently runs on the unrestricted worker.

Resolution is deliberately late and degradable. ``sandbox_queue()`` returns the
*default* queue when ``UL_SANDBOX_ENABLED`` is off, so an install that never
started a ``media-worker`` container keeps working (with the isolation it
didn't ask for absent) rather than accumulating a queue nobody drains.
"""

from __future__ import annotations

from enum import StrEnum


class Queue(StrEnum):
    """A Celery queue name.

    Attributes:
        DEFAULT: Celery's own default queue name. Anything without an explicit
            queue lands here, drained by ``celery-worker``.
        PANEL_FETCH: External-data panel fetches for the pin detail page.
        SANDBOX: Interactive parsing of untrusted user-supplied bytes - image
            decode, video transcode, document conversion. Drained by
            ``media-worker``, which has no route to the internet.
        SANDBOX_BATCH: Long-running untrusted-parse batch jobs - archive walks,
            data imports. Same isolation as :attr:`SANDBOX`, its own worker, so
            an hour-long import never queues in front of a photo upload.
        AI_INFERENCE: Reserved for model inference once it moves to its own
            container. Nothing routes here yet; the name exists so the split
            is a compose change rather than a code change.
    """

    DEFAULT = "celery"
    PANEL_FETCH = "panel_fetch"
    SANDBOX = "sandbox"
    SANDBOX_BATCH = "sandbox_batch"
    AI_INFERENCE = "ai_inference"


def sandbox_queue(*, batch: bool = False) -> str:
    """The queue untrusted-parse tasks should be routed to.

    Read once per task definition, at import time, so it appears in the task's
    own exec options rather than at each call site.

    Args:
        batch: True for a task that runs for minutes rather than for a moment -
            it goes to :attr:`Queue.SANDBOX_BATCH` so it cannot occupy the
            interactive pool. Same isolation either way.

    Returns:
        The matching sandbox queue when a sandbox worker is deployed, else
        :attr:`Queue.DEFAULT` - an install with no ``media-worker`` container
        keeps processing uploads on the ordinary worker instead of enqueuing
        into a queue that nothing drains.
    """
    from django.conf import settings

    if not getattr(settings, "UL_SANDBOX_ENABLED", False):
        return Queue.DEFAULT
    return Queue.SANDBOX_BATCH if batch else Queue.SANDBOX
