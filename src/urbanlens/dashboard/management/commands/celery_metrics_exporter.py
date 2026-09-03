"""Consume Celery's event stream and expose it as Prometheus metrics.

Runs as its own long-lived service (``celery-metrics`` in docker-compose.yml).
It has to be a separate process rather than a thread in an existing one: the web
tier runs ``WEB_CONCURRENCY`` gunicorn workers and each would open its own
receiver and triple-count every event, and the Celery workers themselves cannot
be scraped - ``media-worker`` and ``media-worker-batch`` run ``cap_drop: ALL`` on
an isolated network specifically so they have no inbound surface.

Single process, so the default ``prometheus_client`` registry is correct here
and multiprocess mode is deliberately not used - the counters live in this
process's memory and are lost on restart, which is the normal and correct
behaviour for a Prometheus counter (``rate()`` handles resets).

Serves on a port that is *not* published to the host: only the compose network
reaches it, and Alloy joins that network to scrape. The bearer-token gate is
applied anyway, through the same
:mod:`~urbanlens.dashboard.services.core.metrics_auth` the web endpoint uses -
one implementation, two transports, so neither can quietly become the open one.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import threading
from typing import Any

from celery import current_app
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
import prometheus_client

from urbanlens.dashboard.services.core.celery_events import CeleryEventMetrics
from urbanlens.dashboard.services.core.metrics_auth import gates_configured, network_ok, token_ok

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8002


class Command(BaseCommand):
    """Run the Celery event receiver and the metrics HTTP server."""

    help = "Consume Celery worker events and expose them as Prometheus metrics."

    def add_arguments(self, parser: Any) -> None:
        """Register command-line options.

        Args:
            parser: The argument parser Django provides.
        """
        parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to serve /metrics on (default {DEFAULT_PORT}).")
        parser.add_argument("--bind", default="0.0.0.0", help="Address to bind (default 0.0.0.0; the port is not published to the host).")  # noqa: S104

    def handle(self, *args: Any, **options: Any) -> None:
        """Serve metrics and consume events until interrupted.

        Args:
            *args: Unused.
            **options: Parsed command-line options.

        Raises:
            CommandError: If metrics are disabled, or enabled without a gate on
                a deployed environment - the same condition
                ``dashboard.E006`` raises for the web endpoint. Refusing to
                start is better than binding an unguarded port and logging
                about it.
        """
        if not settings.UL_METRICS_ENABLED:
            raise CommandError("UL_METRICS_ENABLED is off; this exporter has nothing to serve. Enable it or do not deploy this service.")
        if getattr(settings, "IS_PRODUCTION", False) and not gates_configured():
            raise CommandError("UL_METRICS_ENABLED is on with neither UL_METRICS_TOKEN nor UL_METRICS_ALLOWED_CIDRS set. Refusing to expose an unguarded metrics port.")

        metrics = CeleryEventMetrics(known_tasks=current_app.tasks.keys(), registry=prometheus_client.REGISTRY)
        self._serve(options["bind"], options["port"], metrics)
        self._consume(metrics)

    def _serve(self, bind: str, port: int, metrics: CeleryEventMetrics) -> None:
        """Start the metrics HTTP server on a daemon thread.

        Args:
            bind: Address to bind.
            port: Port to listen on.
            metrics: The metric set whose registry is served.
        """
        handler = _build_handler(metrics)
        server = ThreadingHTTPServer((bind, port), handler)
        thread = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
        thread.start()
        self.stdout.write(f"Serving Celery metrics on {bind}:{port}/metrics")

    def _consume(self, metrics: CeleryEventMetrics) -> None:
        """Block on the Celery event stream, feeding events into metrics.

        Reconnects on broker errors rather than exiting, because this service
        losing its broker is a transient condition that restarting the container
        would not fix any faster - and an exporter that dies on a blip takes the
        metrics with it exactly when something is going wrong.

        Args:
            metrics: The metric set to feed.
        """
        state = current_app.events.State()

        def on_event(event: dict[str, Any]) -> None:
            state.event(event)
            # Only task-received carries the task name; every later event for
            # the same task refers to it by uuid, so the name is looked up in
            # the state Celery maintains for exactly this reason.
            task = state.tasks.get(event.get("uuid", ""))
            metrics.on_task_event(event, getattr(task, "name", None))
            metrics.on_worker_count(sum(1 for worker in state.workers.values() if worker.alive))

        while True:
            try:
                with current_app.connection() as connection:
                    receiver = current_app.events.Receiver(connection, handlers={"*": on_event})
                    self.stdout.write("Connected to the Celery event stream")
                    receiver.capture(limit=None, timeout=None, wakeup=True)
            except KeyboardInterrupt:
                self.stdout.write("Stopping")
                return
            except Exception:
                logger.warning("Celery event stream dropped; reconnecting", exc_info=True)


def _build_handler(metrics: CeleryEventMetrics) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to a metric set.

    Args:
        metrics: The metric set whose registry is served.

    Returns:
        A ``BaseHTTPRequestHandler`` subclass serving ``/metrics``.
    """

    class MetricsHandler(BaseHTTPRequestHandler):
        """Serve the exposition format to an authorized scraper."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            """Handle a scrape, applying the same gates as the web endpoint."""
            if self.path.split("?")[0] != "/metrics":
                self._respond(404, b"Not Found", "text/plain")
                return
            # No proxy in front of this port, so the socket address is the
            # client - there is no X-Forwarded-For to count hops through.
            if not network_ok(self.client_address[0]):
                logger.warning("Refused Celery metrics scrape from disallowed address %r", self.client_address[0])
                self._respond(404, b"Not Found", "text/plain")
                return
            if not token_ok(self.headers.get("Authorization", "")):
                logger.warning("Refused Celery metrics scrape with an invalid token from %r", self.client_address[0])
                self._respond(401, b"Unauthorized", "text/plain", extra={"WWW-Authenticate": "Bearer"})
                return

            payload = prometheus_client.generate_latest(metrics.registry)
            self._respond(200, payload, prometheus_client.CONTENT_TYPE_LATEST)

        def _respond(self, status: int, body: bytes | str, content_type: str, extra: dict[str, str] | None = None) -> None:
            """Write one complete response.

            Args:
                status: HTTP status code.
                body: Response body.
                content_type: Value for the Content-Type header.
                extra: Additional headers.
            """
            payload = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature fixed by the base class
            """Route access logging to the app's logger instead of stderr.

            Args:
                format: Printf-style format string.
                *args: Format arguments.
            """
            logger.debug("celery-metrics %s", format % args)

    return MetricsHandler
