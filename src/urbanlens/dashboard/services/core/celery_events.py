"""Turning Celery's event stream into Prometheus metrics.

Per-task outcome and duration need the worker to say what it did, and the
workers cannot be scraped: ``media-worker`` and ``media-worker-batch`` run
``cap_drop: ALL`` on an isolated network specifically so they have no inbound
surface. Celery's event stream solves that in the direction that already works -
every worker publishes to the broker it is already connected to, and one
consumer elsewhere turns those events into metrics.

This module is only the translation. The transport (the event receiver loop and
the HTTP server) lives in the ``celery_metrics_exporter`` management command, so
the interesting logic here is exercisable without a broker or a socket.

Cardinality is the thing to be careful about: ``task`` is a label, and an
unbounded set of label values is how a Prometheus instance falls over. Names are
therefore learned from the stream but capped - the first
:data:`MAX_TASK_LABELS` distinct names get their own series and everything after
that collapses into :data:`OVERFLOW`.

Checking names against the app's own task registry would be the more precise
guard, and is what this did first, but it is not affordable here: populating that
registry means importing every task module (GDAL, Pillow, GeoPandas and the rest
arrive with them), which measured at +167 MiB against an exporter whose whole
container budget is a few hundred. Doubling a process's memory to learn ninety-
odd strings is the wrong trade when a counter bounds the same risk for free.
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

#: Label value for an event whose task name could not be resolved - only
#: ``task-received`` carries the name, so an event arriving before it, or after a
#: restart dropped the receiver's state, has nothing to report.
UNKNOWN = "unknown"

#: Label value for every task name past :data:`MAX_TASK_LABELS`. Distinct from
#: :data:`UNKNOWN`: this one means the cap was hit, which is worth being able to
#: see rather than blending into "name missing".
OVERFLOW = "other"

#: How many distinct task names get their own series. Comfortably above the ~95
#: this deployment registers, so the cap is a backstop against a worker on a
#: different build or a dynamically-named task rather than a limit reached in
#: normal operation.
MAX_TASK_LABELS = 200

#: Buckets for task runtime. Wider at the top than the HTTP histogram: a request
#: that takes a minute is broken, whereas an archive import legitimately runs for
#: the best part of an hour, and CELERY_TASK_TIME_LIMIT (3600s) is where a task
#: is killed - so that boundary is a bucket, to separate "slow" from "hit the
#: limit".
RUNTIME_BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, float("inf"))


class CeleryEventMetrics:
    """Prometheus metrics fed by Celery worker events.

    Holds its own registry rather than using the global default, so a test can
    build one per case and assert on it without the leakage that makes
    module-level metrics awkward to test.
    """

    def __init__(self, registry: CollectorRegistry | None = None, max_task_labels: int = MAX_TASK_LABELS) -> None:
        """Build the metric families.

        Args:
            registry: Registry to attach to; a fresh one when omitted.
            max_task_labels: How many distinct task names may hold their own
                series before the rest collapse into :data:`OVERFLOW`.
        """
        self.registry = registry if registry is not None else CollectorRegistry()
        self._max_task_labels = max_task_labels
        self._seen_tasks: set[str] = set()

        self.tasks_total = Counter(
            "urbanlens_celery_tasks_total",
            "Celery task outcomes, by task name and terminal state.",
            ["task", "state"],
            registry=self.registry,
        )
        self.task_runtime = Histogram(
            "urbanlens_celery_task_runtime_seconds",
            "Task execution time as reported by the worker that ran it.",
            ["task"],
            buckets=RUNTIME_BUCKETS,
            registry=self.registry,
        )
        self.workers_online = Gauge(
            "urbanlens_celery_workers_online",
            "Workers currently sending heartbeats.",
            registry=self.registry,
        )
        self.events_total = Counter(
            "urbanlens_celery_events_total",
            "Celery events consumed by this exporter, by event type.",
            ["type"],
            registry=self.registry,
        )

    def task_label(self, name: str | None) -> str:
        """Map a task name from the wire onto a bounded label value.

        Args:
            name: Task name as the worker reported it, possibly ``None`` when
                the event arrived before the name was known.

        Returns:
            :data:`UNKNOWN` when there is no name, the name itself while fewer
            than ``max_task_labels`` distinct names have been seen, and
            :data:`OVERFLOW` once that cap is reached. Names already admitted
            keep their series, so the cap freezes the label set rather than
            starting to drop tasks that were already being reported.
        """
        if not name:
            return UNKNOWN
        if name in self._seen_tasks:
            return name
        if len(self._seen_tasks) >= self._max_task_labels:
            logger.warning("Celery task-name cardinality cap (%d) reached; reporting %r as %r", self._max_task_labels, name, OVERFLOW)
            return OVERFLOW
        self._seen_tasks.add(name)
        return name

    def on_task_event(self, event: dict[str, Any], task_name: str | None) -> None:
        """Record one task event.

        Args:
            event: The raw Celery event mapping. ``type`` names the transition;
                ``runtime`` is present on ``task-succeeded``.
            task_name: Resolved task name for the task this event belongs to,
                which the caller looks up in Celery's ``State`` because only the
                ``task-received`` event carries it.
        """
        event_type = str(event.get("type", ""))
        self.events_total.labels(type=event_type or "unknown").inc()

        state = _TERMINAL_STATES.get(event_type)
        if state is None:
            return

        label = self.task_label(task_name)
        self.tasks_total.labels(task=label, state=state).inc()

        if state == "succeeded":
            runtime = event.get("runtime")
            # Only succeeded events carry a runtime. A failed task's duration is
            # not comparable to a successful one's - it is the time to the
            # exception - so it is deliberately not folded into the same
            # histogram, where it would drag the latency percentiles toward
            # whatever the failure mode happened to cost.
            if isinstance(runtime, (int, float)):
                self.task_runtime.labels(task=label).observe(float(runtime))

    def on_worker_count(self, count: int) -> None:
        """Record how many workers are currently heartbeating.

        Args:
            count: Number of workers Celery's state considers alive.
        """
        self.workers_online.set(count)


#: Event types that end a task, mapped to the ``state`` label they record.
#: ``task-received`` and ``task-started`` are deliberately absent: they are
#: transitions, not outcomes, and counting them would double-count every task.
_TERMINAL_STATES: dict[str, str] = {
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-rejected": "rejected",
    "task-revoked": "revoked",
    "task-retried": "retried",
}
