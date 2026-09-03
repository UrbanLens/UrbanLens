"""Celery queue depth, exported on the app's ``/metrics``.

Queue depth is the Celery number worth alerting on: it answers "are the workers
keeping up", and it goes wrong in the direction that matters (a queue nothing
drains grows without bound and the symptom is a feature that silently stopped
working). Each queue here is drained by exactly one container - see
:mod:`urbanlens.dashboard.services.sandbox.queues` - so a rising depth names the
container to look at.

Collected at scrape time from the broker rather than instrumented in the
workers, which is what lets it cover the sandbox workers too: they run
``cap_drop: ALL`` on an isolated network precisely so they have no inbound
surface, and giving them an HTTP endpoint to scrape would undo that. They talk
to the broker already, so the broker is where their backlog is visible.

This is deliberately not task-level instrumentation. Per-task success, failure
and duration need the worker to report what it did - an event stream or a
push - and that is a separate piece of infrastructure, not a bigger version of
this one. See docs/METRICS.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from celery import current_app
from kombu.exceptions import KombuError
from prometheus_client.core import GaugeMetricFamily

from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Seconds to wait on the broker during a scrape. A scrape is not allowed to
#: become the slow thing: Prometheus times out on its own and a hung collector
#: would hold a gunicorn worker for the duration.
_BROKER_TIMEOUT_SECONDS = 2.0


class CeleryQueueDepthCollector:
    """Report the number of messages waiting on each queue.

    A ``prometheus_client`` collector rather than module-level gauges, because
    the value only exists at scrape time - there is no event in the web process
    that would update a gauge, and a stale gauge is worse than none.

    Registered by the exporter view rather than at import, so a process that
    does not serve ``/metrics`` never opens a broker connection for it.
    """

    #: Label values are the members of :class:`Queue`, so cardinality is fixed
    #: by the code rather than by anything a request can influence.
    QUEUES: tuple[Queue, ...] = tuple(Queue)

    def collect(self) -> Iterator[GaugeMetricFamily]:
        """Yield the queue-depth gauges for this scrape.

        Yields:
            ``urbanlens_celery_queue_depth`` labelled by queue, and
            ``urbanlens_celery_broker_up`` so a scrape can tell "every queue is
            empty" apart from "the broker did not answer" - which otherwise look
            identical, and mean opposite things.
        """
        up = GaugeMetricFamily("urbanlens_celery_broker_up", "1 when the Celery broker answered this scrape, 0 when it did not.")
        depth = GaugeMetricFamily("urbanlens_celery_queue_depth", "Messages waiting on each Celery queue, by queue name.", labels=["queue"])

        depths = self._read_depths()
        if depths is None:
            up.add_metric([], 0.0)
            # No depth samples at all, rather than zeros. Publishing 0 for every
            # queue when the broker is unreachable would read as a healthy idle
            # system and silence exactly the alert that should fire.
            yield up
            return

        up.add_metric([], 1.0)
        for queue, size in depths.items():
            depth.add_metric([queue], float(size))
        yield up
        yield depth

    def _read_depths(self) -> dict[str, int] | None:
        """Ask the broker how many messages are waiting on each queue.

        Returns:
            Mapping of queue name to depth, or ``None`` when the broker could
            not be reached or the transport cannot answer the question.
        """
        try:
            with current_app.connection_for_read(transport_options={"socket_timeout": _BROKER_TIMEOUT_SECONDS}) as connection:
                channel = connection.default_channel
                # _size is kombu's own per-transport primitive for this, which
                # is why it is used in preference to reaching for a redis
                # client: the memory:// transport used by the test settings
                # implements it too, so this stays exercisable without a broker.
                sizer = getattr(channel, "_size", None)
                if sizer is None:
                    logger.debug("Broker transport %r cannot report queue depth", type(channel).__name__)
                    return None
                return {queue.value: int(sizer(queue.value)) for queue in self.QUEUES}
        except (KombuError, OSError, AttributeError, ValueError):
            # Broad on purpose. This runs inside a scrape, and a broker problem
            # must surface as broker_up=0 - a raised exception here would fail
            # the whole /metrics response, taking the request metrics down with
            # it and turning a Celery problem into a total observability outage.
            logger.warning("Celery queue depth unavailable for this scrape", exc_info=True)
            return None
