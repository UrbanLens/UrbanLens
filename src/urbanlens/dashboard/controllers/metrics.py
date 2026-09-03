"""The Prometheus scrape endpoint.

Reached only when ``UL_METRICS_ENABLED`` is set: the route is registered
conditionally in the project urlconf, so a deployment that has not opted in has
no ``/metrics`` to find rather than a view that decides to refuse. That is the
same reasoning the demo-login route uses, and it matters more here, because
what this endpoint hands out is a map of the application - every view name that
has served a request, its rate, its error rate and its latency.

Two independent gates, either of which may be used alone and which are checked
together when both are configured:

``UL_METRICS_TOKEN``
    An ``Authorization: Bearer <token>`` the scraper must present, compared in
    constant time.

``UL_METRICS_ALLOWED_CIDRS``
    Networks the request must come from, resolved through the same
    trusted-proxy hop counting the rate limiters use, so a forged
    ``X-Forwarded-For`` cannot spoof its way in.

Enabling the endpoint in staging or production with neither configured is a
startup error - see :func:`urbanlens.dashboard.checks.check_metrics_endpoint_is_guarded`.

Multiprocess aggregation is the subtle part; see :func:`_build_registry`.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from django.http import HttpResponse
from django.views import View
import prometheus_client
from prometheus_client import multiprocess

from urbanlens.dashboard.services.core.metrics_auth import network_ok, token_ok
from urbanlens.dashboard.services.security.client_ip import client_ip

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Environment variable ``prometheus_client`` itself reads to decide whether it
#: is in multiprocess mode. Named here rather than inlined because the entrypoint
#: and the gunicorn hooks have to agree with it exactly.
MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"

#: Whether the scrape-time collectors have been attached to the *default*
#: registry yet. Only the single-process path needs this: that registry is a
#: module global that outlives the request, so a second registration would raise
#: Duplicated timeseries. The multiprocess path builds a fresh registry per
#: scrape and has nothing to guard against.
_DEFAULT_REGISTRY_EXTRAS: list[bool] = []


def _build_registry() -> prometheus_client.CollectorRegistry:
    """Return the registry to serialize for this scrape.

    Under gunicorn the web service runs ``WEB_CONCURRENCY`` worker processes
    (3 by default) and a scrape is answered by whichever one the arbiter
    happened to route it to. ``prometheus_client``'s default registry lives in
    process memory, so serving that registry would report one worker's counters
    as though they were the service's - an endpoint that answers, returns
    plausible numbers, and undercounts by roughly the worker count. Multiprocess
    mode instead has every worker write its samples to files in
    ``PROMETHEUS_MULTIPROC_DIR`` and aggregates them here at scrape time.

    Falls back to the default registry when that variable is unset, which is the
    correct behaviour for a genuinely single-process server (``runserver``, a
    test) and the only case where the default registry is not a silent
    undercount.

    Returns:
        A registry whose ``generate_latest`` output covers every process in
        this service.
    """
    multiproc_dir = os.environ.get(MULTIPROC_DIR_ENV)
    if not multiproc_dir:
        registry = prometheus_client.REGISTRY
        # Registered once on the default registry, which persists for the life
        # of the process - registering per scrape would raise Duplicated.
        if not _DEFAULT_REGISTRY_EXTRAS:
            _register_scrape_time_collectors(registry)
            _DEFAULT_REGISTRY_EXTRAS.append(True)
        return registry

    registry = prometheus_client.CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=multiproc_dir)
    # MultiProcessCollector reads the workers' sample files and nothing else, so
    # a collector registered on the default registry is invisible in
    # multiprocess mode - the failure being avoided is a collector that works
    # under runserver and silently disappears in production. These are
    # scrape-time reads with no per-process state, so building them fresh on
    # this registry is correct rather than a workaround.
    _register_scrape_time_collectors(registry)
    return registry


def _register_scrape_time_collectors(registry: prometheus_client.CollectorRegistry) -> None:
    """Attach collectors that compute their values during the scrape.

    Args:
        registry: The registry this scrape will serialize.
    """
    from urbanlens.dashboard.services.core.celery_metrics import CeleryQueueDepthCollector

    registry.register(CeleryQueueDepthCollector())


class MetricsController(View):
    """Serve the Prometheus text exposition format to an authorized scraper.

    A plain ``View`` rather than a DRF viewset: this response has no
    negotiation, no serializer and no session, and DRF's authentication would
    only add a code path that could grant access some way other than the two
    gates below.

    Read-only methods only, so there is nothing for CSRF to protect and no
    ``csrf_exempt`` to add. Django maps HEAD onto ``get``.
    """

    http_method_names = ["get", "head"]

    def get(self, request: HttpRequest) -> HttpResponse:
        """Return the current metrics, or refuse.

        Args:
            request: The incoming scrape request.

        Returns:
            ``200`` with the exposition text when both configured gates pass,
            ``401`` when the bearer token is missing or wrong, and ``404`` when
            the client's address is not in the allowlist - a wrong network is
            told nothing about whether this URL exists at all.
        """
        if not self._network_allowed(request):
            logger.warning("Refused /metrics scrape from disallowed address %r", client_ip(request))
            return HttpResponse("Not Found", status=404, content_type="text/plain")
        if not self._token_valid(request):
            logger.warning("Refused /metrics scrape with an invalid token from %r", client_ip(request))
            response = HttpResponse("Unauthorized", status=401, content_type="text/plain")
            response["WWW-Authenticate"] = "Bearer"
            return response

        payload = prometheus_client.generate_latest(_build_registry())
        response = HttpResponse(payload, content_type=prometheus_client.CONTENT_TYPE_LATEST)
        # Metrics are a point-in-time reading; a cache between here and the
        # scraper replaying one would show a stalled service as a healthy one.
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _network_allowed(request: HttpRequest) -> bool:
        """Check the request's source address against the configured CIDRs.

        Args:
            request: The incoming scrape request.

        Returns:
            ``True`` when no allowlist is configured (the gate is off) or the
            resolved client address falls inside one of its networks.
        """
        return network_ok(client_ip(request))

    @staticmethod
    def _token_valid(request: HttpRequest) -> bool:
        """Check the request's bearer token against the configured one.

        Args:
            request: The incoming scrape request.

        Returns:
            ``True`` when no token is configured (the gate is off) or the
            presented token matches.
        """
        return token_ok(request.headers.get("Authorization", ""))
