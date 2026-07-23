"""Shared Celery helpers for queueing work and reporting progress."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from celery import current_app
from celery.result import AsyncResult
from kombu.exceptions import KombuError

logger = logging.getLogger(__name__)

PROGRESS_STATE = "PROGRESS"


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Serializable task status payload for progress-bar UIs."""

    task_id: str
    state: str
    current: int = 0
    total: int = 1
    percent: int = 0
    message: str = ""
    result: Any | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "current": self.current,
            "total": self.total,
            "percent": self.percent,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "ready": self.state in {"SUCCESS", "FAILURE", "REVOKED"},
        }


def update_task_progress(task: Any, *, current: int, total: int, message: str = "") -> None:
    """Update Celery task metadata in a consistent progress format."""
    safe_total = max(int(total or 1), 1)
    safe_current = max(0, min(int(current or 0), safe_total))
    percent = int((safe_current / safe_total) * 100)
    task.update_state(
        state=PROGRESS_STATE,
        meta={
            "current": safe_current,
            "total": safe_total,
            "percent": percent,
            "message": message,
        },
    )


def get_task_progress(task_id: str) -> TaskProgress:
    """Return normalized task status for polling clients."""
    result = AsyncResult(task_id, app=current_app)
    state = result.state
    info = result.info if isinstance(result.info, dict) else {}

    if state == "SUCCESS":
        return TaskProgress(task_id=task_id, state=state, current=1, total=1, percent=100, result=result.result)
    if state in {"FAILURE", "REVOKED"}:
        error = str(result.result or result.info or "Task failed")
        return TaskProgress(task_id=task_id, state=state, error=error)

    current = int(info.get("current") or 0)
    total = int(info.get("total") or 1)
    percent = int(info.get("percent") or 0)
    message = str(info.get("message") or "")
    return TaskProgress(task_id=task_id, state=state, current=current, total=total, percent=percent, message=message)


def safely_enqueue_task(task: Any, *args: Any, countdown: int | None = None, queue: str | None = None, **kwargs: Any) -> AsyncResult | None:
    """Queue a Celery task with consistent logging and broker exception handling.

    Args:
        task: The Celery task to enqueue.
        *args: Positional arguments passed to the task.
        countdown: Seconds to delay execution, if any.
        queue: Celery queue to dispatch to; None uses the task's default route.
        **kwargs: Keyword arguments passed to the task.

    Returns:
        The AsyncResult on success, or None when the broker was unreachable.
    """
    try:
        apply_kwargs: dict[str, Any] = {}
        if countdown is not None:
            apply_kwargs["countdown"] = countdown
        if queue is not None:
            apply_kwargs["queue"] = queue
        return task.apply_async(args=args, kwargs=kwargs, **apply_kwargs)
    except (KombuError, ConnectionError, OSError, RuntimeError):
        logger.exception("Unable to enqueue Celery task %s", getattr(task, "name", task))
        return None
