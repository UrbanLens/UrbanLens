"""Health check endpoints for Docker, Kubernetes and load-balancer probes.

Cheap enough to probe every few seconds, and never fails because a dependency is unwell - a liveness
probe that fails on a database blip gets the container killed for no reason.
Database and cache are reachable, and it reports how much of the connection pool is left - see
``_probe_connections`` for why that is a field rather than a failure.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, HttpResponse, JsonResponse
from rest_framework.permissions import AllowAny
from rest_framework.viewsets import GenericViewSet

logger = logging.getLogger(__name__)

# Probes run often and must not become the thing that takes a site down, so a dependency that has not answered
# by now counts as unreachable rather than holding the worker.
_PROBE_TIMEOUT_SECONDS = 2

#: Fraction of the database's connection ceiling above which this instance calls itself degraded.
_CONNECTION_PRESSURE_FRACTION = 0.8

_CACHE_PROBE_KEY = "health:probe"


class HealthController(GenericViewSet):
    """Unauthenticated probes.

    They report which dependency is unwell and whether this database is a primary, which is operational
    detail rather than anything sensitive, but there is no reason to hand out more than a prober needs.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    def check(self, request: HttpRequest) -> HttpResponse:
        """Return a plain 200 so probes can treat any other status as failure.

        Args:
            request: Incoming HTTP request (unused).

        Returns:
            ``HttpResponse`` with body ``Okay!`` and status 200.
        """
        return HttpResponse("Okay!", status=200)

    def live(self, request: HttpRequest) -> HttpResponse:
        """Liveness probe: the process is up and serving.

        Args:
            request: Incoming HTTP request (unused).

        Returns:
            ``HttpResponse`` with body ``Okay!`` and status 200.
        """
        return HttpResponse("Okay!", status=200)

    def ready(self, request: HttpRequest) -> JsonResponse:
        """Readiness probe: dependencies this instance needs are reachable.

        Args:
            request: Incoming HTTP request (unused).

        Returns:
            ``JsonResponse`` describing each dependency, with status 200 when the database and cache both
            answered and 503 otherwise.
        """
        report = self._collect()
        healthy = report["db"] == "ok" and report["cache"] == "ok"
        return JsonResponse(report, status=200 if healthy else 503)

    def primary(self, request: HttpRequest) -> JsonResponse:
        """Writable probe: dependencies are reachable *and* this DB accepts writes.

        Args:
            request: Incoming HTTP request (unused).

        Returns:
            ``JsonResponse`` describing each dependency, with status 200 only when this instance can serve
            writes, and 503 otherwise (including when...
        """
        report = self._collect()
        healthy = report["db"] == "ok" and report["cache"] == "ok" and report["role"] == "primary"
        return JsonResponse(report, status=200 if healthy else 503)

    def _collect(self) -> dict[str, Any]:
        """Probe every dependency once and describe what came back.

        Returns:
            Mapping with ``db``, ``cache``, ``role`` and ``migrations`` keys.
        """
        db_status, role = self._probe_database()
        cache_status = self._probe_cache()
        connections = self._probe_connections() if db_status == "ok" else None
        report: dict[str, Any] = {
            "db": db_status,
            "cache": cache_status,
            "role": role,
            "migrations": self._probe_migrations() if db_status == "ok" else "unknown",
            "connections": connections,
        }
        report["degraded"] = self._is_degraded(cache_status=cache_status, db_status=db_status, connections=connections)
        return report

    @staticmethod
    def _is_degraded(*, cache_status: str, db_status: str, connections: dict[str, int] | None) -> bool:
        """Whether this instance is serving but should be looked at.

        Distinct from unhealthy on purpose.

        Args:
            cache_status: What the cache probe reported.
            db_status: What the database probe reported.
            connections: Pool usage, or None when it could not be read.

        Returns:
            True when the instance is up but not well.
        """
        if db_status == "ok" and cache_status != "ok":
            return True
        if connections is None:
            return False
        used, maximum = connections.get("used", 0), connections.get("max", 0)
        return bool(maximum) and used >= maximum * _CONNECTION_PRESSURE_FRACTION

    @staticmethod
    def _probe_connections() -> dict[str, int] | None:
        """How much of the database's connection ceiling is in use.

        Alert on the field instead.

        Returns:
            ``{"used": n, "max": m}``, or None when the database is not PostgreSQL or the counts could not
            be read.
        """
        if connection.vendor != "postgresql":
            return None
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = %s", [_PROBE_TIMEOUT_SECONDS * 1000])
                cursor.execute(
                    "SELECT (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'), current_setting('max_connections')::int",
                )
                used, maximum = cursor.fetchone()
        except DatabaseError:
            # Not a failure of the probe: pg_stat_activity needs pg_read_all_stats (or superuser), and a
            # deployment that has dropped that privilege should still report ready rather than error.
            logger.warning("Health probe: could not read connection usage", exc_info=True)
            return None
        return {"used": int(used), "max": int(maximum)}

    def _probe_database(self) -> tuple[str, str]:
        """Check the database answers, and whether it is a writable primary.

        Returns:
            ``(status, role)`` where status is ``ok`` or ``error`` and role is ``primary``, ``replica`` or
            ``unknown``.
        """
        try:
            if connection.vendor == "postgresql":
                # SET LOCAL only applies inside a transaction, which is also what scopes it: a session-level SET
                # would outlive the probe on a persistent connection (CONN_MAX_AGE) and silently cap every
                # subsequent real query at the probe's timeout.
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "SET LOCAL statement_timeout = %s",
                        [_PROBE_TIMEOUT_SECONDS * 1000],
                    )
                    cursor.execute("SELECT pg_is_in_recovery()")
                    in_recovery = cursor.fetchone()[0]
                    return "ok", "replica" if in_recovery else "primary"
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except DatabaseError:
            logger.warning("Health probe: database unreachable", exc_info=True)
            return "error", "unknown"
        return "ok", "unknown"

    def _probe_cache(self) -> str:
        """Check the cache backend accepts a write and returns it.

        Returns:
            ``ok`` if the round trip succeeded, ``error`` otherwise.
        """
        try:
            cache.set(_CACHE_PROBE_KEY, "1", timeout=_PROBE_TIMEOUT_SECONDS)
            if cache.get(_CACHE_PROBE_KEY) != "1":
                logger.warning("Health probe: cache round trip did not return the written value")
                return "error"
        except Exception:
            logger.warning("Health probe: cache unreachable", exc_info=True)
            return "error"
        return "ok"

    def _probe_migrations(self) -> str:
        """Report whether unapplied migrations exist.

        During a rolling multi-site deploy an instance legitimately runs briefly against a schema its own
        image has not caught up to, and failing readiness here would take the last serving site out
        precisely when it is needed.

        Returns:
            ``current``, ``behind``, or ``unknown`` if the check itself failed.
        """
        try:
            executor = MigrationExecutor(connection)
            targets = executor.loader.graph.leaf_nodes()
            return "behind" if executor.migration_plan(targets) else "current"
        except Exception:
            logger.warning("Health probe: could not determine migration state", exc_info=True)
            return "unknown"
