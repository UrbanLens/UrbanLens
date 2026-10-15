"""Scheduled background work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.plugins.extensions import ExtensionPoint, Registry

if TYPE_CHECKING:
    from urbanlens.UrbanLens.egress import BeatEgress


BEAT_SCHEDULE: Registry[str, dict[str, Any]] = Registry(
    "celery.beat_schedule",
    "Celery beat entries by name, in ``CELERY_BEAT_SCHEDULE`` shape. Each needs a matching ``celery.beat_egress`` entry.",
)

BEAT_EGRESS: Registry[str, BeatEgress] = Registry(
    "celery.beat_egress",
    "The egress classification of each plugin beat entry, which decides the environments it runs in.",
)

ACKS_LATE_EXEMPT: ExtensionPoint[str] = ExtensionPoint(
    "celery.acks_late_exempt",
    "Task names that run with ``acks_late`` off on purpose: a task that is lost on a dead worker rather than repeated, because re-running it would re-spend a call the caller has given up on.",
)

__all__ = ["ACKS_LATE_EXEMPT", "BEAT_EGRESS", "BEAT_SCHEDULE"]
