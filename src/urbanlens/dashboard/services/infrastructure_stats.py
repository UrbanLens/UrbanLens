"""Health and usage statistics for UrbanLens infrastructure services."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import logging
import os
import time
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from celery import current_app
from django.conf import settings as django_settings
from django.db import connection
from kombu.exceptions import KombuError
import redis
from redis.exceptions import RedisError
import requests

logger = logging.getLogger(__name__)

ServiceStatus = Literal["healthy", "unhealthy", "unavailable", "disabled"]


@dataclass(frozen=True, slots=True)
class ServiceMetric:
    """A single labeled metric shown on the admin stats page."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class InfrastructureServiceStat:
    """Collected health and usage data for one infrastructure component."""

    key: str
    name: str
    icon: str
    status: ServiceStatus
    status_label: str
    metrics: tuple[ServiceMetric, ...]


def _format_duration(seconds: float) -> str:
    """Format ``seconds`` as a compact days/hours/minutes string."""
    total = max(0, int(seconds))
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    return f"{days}d {hours}h {minutes}m"


def _postgres_version_label(version: str) -> str:
    """Return a short Postgres version label from ``version()`` output."""
    if "PostgreSQL" in version:
        for part in version.split():
            if part[0].isdigit():
                return f"PostgreSQL {part}"
    return version.split(",", 1)[0][:48]


def _read_postgres_row() -> tuple[Any, ...]:
    """Query PostgreSQL for database health metrics.

    Returns:
        Row from the infrastructure stats query.

    Raises:
        Exception: Propagates database errors to the caller.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                version(),
                pg_size_pretty(pg_database_size(current_database())),
                (
                    SELECT count(*)::int
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                ),
                current_setting('max_connections')::int,
                EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))::bigint,
                current_database(),
                (
                    SELECT extversion
                    FROM pg_extension
                    WHERE extname = 'postgis'
                    LIMIT 1
                )
            """,
        )
        row = cursor.fetchone()
    if row is None:
        msg = "PostgreSQL stats query returned no rows"
        raise RuntimeError(msg)
    return row


def _build_postgres_metrics(row: tuple[Any, ...]) -> tuple[ServiceMetric, ...]:
    """Build labeled PostgreSQL metrics from a stats query row."""
    version, db_size, connections, max_connections, uptime_seconds, database_name, postgis_version = row
    host = connection.settings_dict.get("HOST") or "localhost"
    port = connection.settings_dict.get("PORT") or "5432"
    metrics = [
        ServiceMetric("Host", f"{host}:{port}"),
        ServiceMetric("Database", str(database_name)),
        ServiceMetric("Version", _postgres_version_label(str(version))),
        ServiceMetric("Size", str(db_size)),
        ServiceMetric("Connections", f"{connections} / {max_connections}"),
        ServiceMetric("Uptime", _format_duration(float(uptime_seconds or 0))),
    ]
    if postgis_version:
        metrics.append(ServiceMetric("PostGIS", str(postgis_version)))
    return tuple(metrics)


def collect_postgres_stats() -> InfrastructureServiceStat:
    """Collect PostgreSQL/PostGIS connection and database statistics.

    Returns:
        InfrastructureServiceStat for the configured Django database.
    """
    metrics: list[ServiceMetric] = []
    try:
        metrics = list(_build_postgres_metrics(_read_postgres_row()))
        return InfrastructureServiceStat(
            key="postgres",
            name="PostgreSQL",
            icon="storage",
            status="healthy",
            status_label="Connected",
            metrics=tuple(metrics),
        )
    except Exception:
        logger.exception("Failed to collect PostgreSQL infrastructure stats")
        return InfrastructureServiceStat(
            key="postgres",
            name="PostgreSQL",
            icon="storage",
            status="unhealthy",
            status_label="Connection error",
            metrics=tuple(metrics),
        )


def _valkey_metrics(client: redis.Redis) -> tuple[ServiceMetric, ...]:
    """Build labeled Valkey metrics from a connected client."""
    info = client.info()
    uptime_seconds = int(info.get("uptime_in_seconds", 0))
    # Valkey reports its version under "valkey_version"; Redis uses "redis_version".
    server_version = str(info.get("valkey_version") or info.get("redis_version") or "Unknown")

    hits = int(info.get("keyspace_hits", 0))
    misses = int(info.get("keyspace_misses", 0))
    total_ops = hits + misses
    hit_rate = f"{hits / total_ops * 100:.1f}%" if total_ops else "n/a"

    evictions = int(info.get("evicted_keys", 0))

    maxmemory_bytes = int(info.get("maxmemory", 0))
    maxmemory_label = info.get("maxmemory_human") or ("unlimited" if maxmemory_bytes == 0 else str(maxmemory_bytes))

    metrics: list[ServiceMetric] = [
        ServiceMetric("Version", server_version),
        ServiceMetric("Uptime", _format_duration(uptime_seconds)),
        ServiceMetric("Memory Used", str(info.get("used_memory_human", "Unknown"))),
        ServiceMetric("Memory Peak", str(info.get("used_memory_peak_human", "Unknown"))),
        ServiceMetric("Memory Limit", maxmemory_label),
        ServiceMetric("Clients", str(info.get("connected_clients", "Unknown"))),
        ServiceMetric("Hit Rate", hit_rate),
        ServiceMetric("Keys", str(client.dbsize())),
    ]
    if evictions:
        metrics.append(ServiceMetric("Evictions", str(evictions)))
    return tuple(metrics)


def collect_valkey_stats() -> InfrastructureServiceStat:
    """Collect Valkey/Redis cache statistics.

    Returns:
        InfrastructureServiceStat for the configured Valkey instance, or a
        disabled stat when ``UL_VALKEY_URL``/``UL_REDIS_URL`` is unset.
    """
    url = os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL")
    if not url:
        return InfrastructureServiceStat(
            key="valkey",
            name="Valkey",
            icon="memory",
            status="disabled",
            status_label="Not configured",
            metrics=(
                ServiceMetric("Status", "UL_VALKEY_URL is not set"),
            ),
        )

    client: redis.Redis | None = None
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
        client.ping()
        return InfrastructureServiceStat(
            key="valkey",
            name="Valkey",
            icon="memory",
            status="healthy",
            status_label="Connected",
            metrics=_valkey_metrics(client),
        )
    except RedisError:
        logger.exception("Failed to collect Valkey infrastructure stats")
        return InfrastructureServiceStat(
            key="valkey",
            name="Valkey",
            icon="memory",
            status="unhealthy",
            status_label="Connection error",
            metrics=(),
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


_CELERY_STATS_CACHE_KEY = "dashboard:infra:celery_stats"
_CELERY_STATS_TTL = 30  # seconds — limits blocking RPCs to at most once per 30s


def collect_celery_stats() -> InfrastructureServiceStat:
    """Collect Celery broker and worker statistics for the admin stats page.

    Results are cached for 30 seconds because each live collection makes up to five
    synchronous broadcast RPCs (ping, stats, active, reserved, scheduled), each with
    a 1-second timeout, which can block an admin page load for up to 5 seconds.
    """
    from django.core.cache import cache

    with contextlib.suppress(Exception):
        cached = cache.get(_CELERY_STATS_CACHE_KEY)
        if cached is not None:
            return cached

    stat = _collect_celery_stats_live()

    with contextlib.suppress(Exception):
        cache.set(_CELERY_STATS_CACHE_KEY, stat, timeout=_CELERY_STATS_TTL)

    return stat


def _collect_celery_stats_live() -> InfrastructureServiceStat:
    """Make live RPC calls to collect Celery worker statistics."""
    broker_url = getattr(django_settings, "CELERY_BROKER_URL", "")
    if not broker_url:
        return InfrastructureServiceStat(
            key="celery",
            name="Celery",
            icon="task_alt",
            status="disabled",
            status_label="Not configured",
            metrics=(ServiceMetric("Status", "CELERY_BROKER_URL is not set"),),
        )

    metrics: list[ServiceMetric] = [
        ServiceMetric("Broker", _redact_url(str(broker_url))),
    ]
    try:
        with current_app.connection_for_read() as connection_for_read:
            connection_for_read.ensure_connection(max_retries=1)
        inspect = current_app.control.inspect(timeout=1)
        ping = inspect.ping() or {}
        stats = inspect.stats() or {}
        active = inspect.active() or {}
        reserved = inspect.reserved() or {}
        scheduled = inspect.scheduled() or {}

        worker_count = len(ping)
        active_count = sum(len(tasks) for tasks in active.values())
        reserved_count = sum(len(tasks) for tasks in reserved.values())
        scheduled_count = sum(len(tasks) for tasks in scheduled.values())
        metrics.extend(
            [
                ServiceMetric("Workers", str(worker_count)),
                ServiceMetric("Active Tasks", str(active_count)),
                ServiceMetric("Reserved Tasks", str(reserved_count)),
                ServiceMetric("Scheduled Tasks", str(scheduled_count)),
            ],
        )
        celery_versions = sorted(
            {str(worker_stats.get("software", {}).get("celery")) for worker_stats in stats.values() if worker_stats.get("software", {}).get("celery")},
        )
        if celery_versions:
            metrics.append(ServiceMetric("Version", ", ".join(celery_versions)))
        status: ServiceStatus = "healthy" if worker_count else "unavailable"
        return InfrastructureServiceStat(
            key="celery",
            name="Celery",
            icon="task_alt",
            status=status,
            status_label="Workers online" if worker_count else "No workers responding",
            metrics=tuple(metrics),
        )
    except (KombuError, OSError, RuntimeError):
        logger.exception("Failed to collect Celery infrastructure stats")
        return InfrastructureServiceStat(
            key="celery",
            name="Celery",
            icon="task_alt",
            status="unhealthy",
            status_label="Connection error",
            metrics=tuple(metrics),
        )


def _redact_url(url: str) -> str:
    """Hide credentials in service URLs displayed to admins."""
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if not parsed.password:
        return url
    host_port = f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "")
    user_part = f"{parsed.username}:***" if parsed.username else "***"
    return urlunparse(parsed._replace(netloc=f"{user_part}@{host_port}"))


def collect_nginx_stats() -> InfrastructureServiceStat:
    """Collect nginx reverse-proxy health statistics.

    Returns:
        InfrastructureServiceStat for the nginx health endpoint configured by
        ``UL_NGINX_HEALTH_URL`` (default ``http://urbanlens_nginx/nginx-health``).
    """
    health_url = os.getenv("UL_NGINX_HEALTH_URL", "http://urbanlens_nginx:8080/nginx-health")
    try:
        started = time.monotonic()
        response = requests.get(health_url, timeout=2)
        latency_ms = round((time.monotonic() - started) * 1000)
        if response.status_code == 200:
            return InfrastructureServiceStat(
                key="nginx",
                name="nginx",
                icon="public",
                status="healthy",
                status_label="Responding",
                metrics=(
                    ServiceMetric("Health URL", health_url),
                    ServiceMetric("Response", f"{response.status_code} OK"),
                    ServiceMetric("Latency", f"{latency_ms} ms"),
                ),
            )
        return InfrastructureServiceStat(
            key="nginx",
            name="nginx",
            icon="public",
            status="unhealthy",
            status_label=f"HTTP {response.status_code}",
            metrics=(
                ServiceMetric("Health URL", health_url),
                ServiceMetric("Response", str(response.status_code)),
            ),
        )
    except (requests.RequestException, OSError):
        logger.debug("nginx health check unavailable at %s", health_url, exc_info=True)
        return InfrastructureServiceStat(
            key="nginx",
            name="nginx",
            icon="public",
            status="unavailable",
            status_label="Unreachable",
            metrics=(
                ServiceMetric("Health URL", health_url),
            ),
        )


def collect_infrastructure_service_stats() -> tuple[InfrastructureServiceStat, ...]:
    """Collect health statistics for all UrbanLens infrastructure services.

    Returns:
        Tuple of service stats in display order: PostgreSQL, Valkey, Celery, nginx.
    """
    return (
        collect_postgres_stats(),
        collect_valkey_stats(),
        collect_celery_stats(),
        collect_nginx_stats(),
    )
