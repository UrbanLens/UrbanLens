"""Tests for infrastructure service statistics collection."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.infrastructure_stats import (
    InfrastructureServiceStat,
    _format_duration,
    _postgres_version_label,
    collect_celery_stats,
    collect_infrastructure_service_stats,
    collect_nginx_stats,
    collect_postgres_stats,
    collect_valkey_stats,
)


class FormatDurationTests(TestCase):
    """_format_duration renders compact uptime strings."""

    def test_formats_days_hours_minutes(self) -> None:
        self.assertEqual(_format_duration(86400 + 7200 + 180), "1d 2h 3m")

    def test_never_returns_negative_values(self) -> None:
        self.assertEqual(_format_duration(-10), "0d 0h 0m")


class PostgresVersionLabelTests(TestCase):
    """_postgres_version_label shortens version() output."""

    def test_extracts_postgresql_version(self) -> None:
        version = "PostgreSQL 16.2 on x86_64-pc-linux-gnu, compiled by gcc"
        self.assertEqual(_postgres_version_label(version), "PostgreSQL 16.2")


class CollectPostgresStatsTests(TestCase):
    """collect_postgres_stats reads live database metrics."""

    def test_returns_healthy_postgres_stat(self) -> None:
        stat = collect_postgres_stats()
        self.assertEqual(stat.key, "postgres")
        self.assertEqual(stat.status, "healthy")
        self.assertGreaterEqual(len(stat.metrics), 5)
        labels = {metric.label for metric in stat.metrics}
        self.assertIn("Database", labels)
        self.assertIn("Connections", labels)


class CollectValkeyStatsTests(TestCase):
    """collect_valkey_stats handles configured and disabled cache backends."""

    def test_returns_disabled_when_url_missing(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            stat = collect_valkey_stats()
        self.assertEqual(stat.status, "disabled")
        self.assertEqual(stat.status_label, "Not configured")

    def test_returns_healthy_when_ping_succeeds(self) -> None:
        fake_client = mock.Mock()
        fake_client.ping.return_value = True
        fake_client.info.return_value = {
            "uptime_in_seconds": 3600,
            "redis_version": "7.2.5",
            "used_memory_human": "1.00M",
            "connected_clients": 2,
        }
        fake_client.dbsize.return_value = 5

        with (
            mock.patch.dict("os.environ", {"UL_VALKEY_URL": "redis://example:6379/0"}),
            mock.patch("urbanlens.dashboard.services.infrastructure_stats.redis.Redis.from_url", return_value=fake_client),
        ):
            stat = collect_valkey_stats()

        self.assertEqual(stat.status, "healthy")
        self.assertEqual(stat.metrics[-1].value, "5")


class CollectCeleryStatsTests(TestCase):
    """collect_celery_stats reports broker and worker availability."""

    def test_returns_disabled_when_broker_missing(self) -> None:
        with mock.patch("urbanlens.dashboard.services.infrastructure_stats.django_settings.CELERY_BROKER_URL", "", create=True):
            stat = collect_celery_stats()
        self.assertEqual(stat.key, "celery")
        self.assertEqual(stat.status, "disabled")

    def test_returns_healthy_when_worker_responds(self) -> None:
        fake_connection = mock.MagicMock()
        fake_inspect = mock.Mock()
        fake_inspect.ping.return_value = {"worker@example": {"ok": "pong"}}
        fake_inspect.stats.return_value = {"worker@example": {"software": {"celery": "5.6.3"}}}
        fake_inspect.active.return_value = {"worker@example": [object()]}
        fake_inspect.reserved.return_value = {"worker@example": []}
        fake_inspect.scheduled.return_value = {"worker@example": []}
        fake_app = mock.Mock()
        fake_app.connection_for_read.return_value.__enter__.return_value = fake_connection
        fake_app.control.inspect.return_value = fake_inspect

        with (
            mock.patch("urbanlens.dashboard.services.infrastructure_stats.django_settings.CELERY_BROKER_URL", "redis://example:6379/0", create=True),
            mock.patch("urbanlens.dashboard.services.infrastructure_stats.current_app", fake_app),
        ):
            stat = collect_celery_stats()

        self.assertEqual(stat.status, "healthy")
        self.assertIn("Workers", {metric.label for metric in stat.metrics})


class CollectNginxStatsTests(TestCase):
    """collect_nginx_stats probes the nginx health endpoint."""

    def test_returns_healthy_on_200_response(self) -> None:
        response = mock.Mock(status_code=200)
        with mock.patch("urbanlens.dashboard.services.infrastructure_stats.requests.get", return_value=response):
            stat = collect_nginx_stats()
        self.assertEqual(stat.status, "healthy")
        self.assertEqual(stat.metrics[1].value, "200 OK")

    def test_returns_unavailable_when_request_fails(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.infrastructure_stats.requests.get",
            side_effect=OSError("connection refused"),
        ):
            stat = collect_nginx_stats()
        self.assertEqual(stat.status, "unavailable")
        self.assertEqual(stat.status_label, "Unreachable")


class CollectInfrastructureServiceStatsTests(TestCase):
    """collect_infrastructure_service_stats returns all expected services."""

    def test_returns_postgres_valkey_celery_and_nginx(self) -> None:
        stats = collect_infrastructure_service_stats()
        self.assertEqual(len(stats), 4)
        self.assertEqual([stat.key for stat in stats], ["postgres", "valkey", "celery", "nginx"])
        for stat in stats:
            self.assertIsInstance(stat, InfrastructureServiceStat)
