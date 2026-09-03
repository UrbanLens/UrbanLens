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
unbounded set of label values is how a Prometheus instance falls over. Task
names come from the worker, not from a request, but a rolling deploy or a
renamed task can still introduce values, so names are checked against this
deployment's own registry and anything unrecognised collapses to a single
``unregistered`` bucket rather than minting a new series.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Label value for a task name this deployment does not have registered. Keeping
#: one bucket rather than the raw name bounds cardinality against a worker
#: running a different build mid-deploy.
UNREGISTERED = "unregistered"

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

    def __init__(self, known_tasks: Iterable[str], registry: CollectorRegistry | None = None) -> None:
        """Build the metric families.

        Args:
            known_tasks: Task names this deployment has registered. Any other
                name seen on the wire is reported as :data:`UNREGISTERED`.
            registry: Registry to attach to; a fresh one when omitted.
        """
        self.registry = registry if registry is not None else CollectorRegistry()
        self._known = frozenset(known_tasks)

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
            The name when this deployment has it registered, else
            :data:`UNREGISTERED`.
        """
        if name and name in self._known:
            return name
        return UNREGISTERED

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
