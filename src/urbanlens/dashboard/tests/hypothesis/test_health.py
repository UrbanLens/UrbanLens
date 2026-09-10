"""Health endpoint and ALLOWED_HOSTS defaults used by Docker healthchecks."""

from __future__ import annotations

import json
import os
from unittest import mock

from django.db import DatabaseError
from django.test import Client, override_settings

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.UrbanLens.settings.app import _default_allowed_hosts

_CONTROLLER = "urbanlens.dashboard.controllers.health.HealthController"


class HealthEndpointTests(TestCase):
    """Docker healthchecks hit /health/ over HTTP without auth or a public Host."""

    def test_unauthenticated_get_returns_200(self) -> None:
        """curl -f against /health/ must succeed for compose healthchecks."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Okay!")

    @override_settings(ALLOWED_HOSTS=["urbanlens.org"])
    def test_localhost_host_is_rejected_when_missing_from_allowed_hosts(self) -> None:
        """Reproduce the staging failure mode: healthcheck Host is localhost."""
        client = Client(SERVER_NAME="localhost")
        response = client.get("/health/")
        self.assertEqual(response.status_code, 400)


class LivenessProbeTests(TestCase):
    """/health/live must behave exactly like /health/ - up means up."""

    def test_returns_200(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Okay!")


class ReadinessProbeTests(TestCase):
    """/health/ready reports dependency reachability."""

    def test_healthy_dependencies_return_200(self) -> None:
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["db"], "ok")
        self.assertEqual(body["cache"], "ok")

    def test_unreachable_database_returns_503(self) -> None:
        with mock.patch(f"{_CONTROLLER}._probe_database", return_value=("error", "unknown")):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["db"], "error")

    def test_unreachable_cache_returns_503(self) -> None:
        with mock.patch(f"{_CONTROLLER}._probe_cache", return_value="error"):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)

    def test_database_error_is_reported_not_raised(self) -> None:
        """A probe must degrade to a 503, never surface a 500."""
        with mock.patch(
            "urbanlens.dashboard.controllers.health.connection.cursor",
            side_effect=DatabaseError("connection refused"),
        ):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["db"], "error")

    def test_pending_migrations_are_advisory_only(self) -> None:
        """A rolling deploy must not take the last serving site out of rotation."""
        with mock.patch(f"{_CONTROLLER}._probe_migrations", return_value="behind"):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["migrations"], "behind")


class PrimaryProbeTests(TestCase):
    """/health/primary is what makes a replica site un-routable for writes."""

    def test_primary_database_returns_200(self) -> None:
        with mock.patch(f"{_CONTROLLER}._probe_database", return_value=("ok", "primary")):
            response = self.client.get("/health/primary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["role"], "primary")

    def test_replica_database_returns_503(self) -> None:
        """A healthy replica is still not somewhere writes can go."""
        with mock.patch(f"{_CONTROLLER}._probe_database", return_value=("ok", "replica")):
            response = self.client.get("/health/primary")
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.content)
        self.assertEqual(body["role"], "replica")
        self.assertEqual(body["db"], "ok")

    def test_replica_is_still_ready(self) -> None:
        """The same instance serves reads fine - only /health/primary refuses it."""
        with mock.patch(f"{_CONTROLLER}._probe_database", return_value=("ok", "replica")):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)


class ProbeAuthenticationTests(TestCase):
    """Probes arrive without credentials from compose, Kubernetes and Cloudflare."""

    def test_all_probes_are_unauthenticated(self) -> None:
        for path in ("/health/", "/health/live", "/health/ready", "/health/primary"):
            with self.subTest(path=path):
                self.assertNotEqual(self.client.get(path).status_code, 403)


class DefaultAllowedHostsTests(SimpleTestCase):
    """Non-local defaults must still allow Docker-internal healthcheck hosts."""

    def test_staging_default_includes_localhost(self) -> None:
        with mock.patch.dict(os.environ, {"UL_ENVIRONMENT": "staging"}, clear=False):
            hosts = _default_allowed_hosts()
        self.assertIn("localhost", hosts)
        self.assertIn("127.0.0.1", hosts)
        self.assertIn("urbanlens.org", hosts)

    def test_production_default_includes_localhost(self) -> None:
        with mock.patch.dict(os.environ, {"UL_ENVIRONMENT": "production"}, clear=False):
            hosts = _default_allowed_hosts()
        self.assertIn("localhost", hosts)
        self.assertIn("127.0.0.1", hosts)


class ConnectionHeadroomTests(TestCase):
    """Readiness must report how much of the connection pool is left."""

    url = "/health/ready"

    def test_it_reports_backends_in_use_and_the_ceiling(self) -> None:
        """The number P104's postmortem needed and nothing exposed.

        The outage read 97 of 100 connections in use, and the first anyone knew
        of it was the site being down; a count turns that into something a scrape
        can watch climb.
        """
        report = json.loads(Client().get(self.url).content)

        connections = report["connections"]
        self.assertIsNotNone(connections, "readiness reported no connection usage against PostgreSQL")
        self.assertGreater(connections["used"], 0, "this very request holds a connection")
        self.assertGreater(connections["max"], 0)
        self.assertLessEqual(connections["used"], connections["max"])

    def test_pressure_is_reported_as_a_field_not_a_status_code(self) -> None:
        """A readiness probe that 503s under connection pressure causes an outage.

        It removes the instances that are still serving, which is the opposite of
        what it is for - and this deployment has done it before. So the endpoint
        keeps answering 200 and something else alerts on `degraded`.
        """
        from urbanlens.dashboard.controllers.health import HealthController

        with mock.patch.object(HealthController, "_probe_connections", return_value={"used": 99, "max": 100}):
            response = Client().get(self.url)

        self.assertEqual(response.status_code, 200, "readiness went unhealthy on connection pressure alone")
        self.assertTrue(json.loads(response.content)["degraded"], "99 of 100 connections was not called degraded")

    def test_a_healthy_pool_is_not_degraded(self) -> None:
        """The negative control: if everything is degraded, nothing is."""
        from urbanlens.dashboard.controllers.health import HealthController

        with mock.patch.object(HealthController, "_probe_connections", return_value={"used": 5, "max": 100}):
            report = json.loads(Client().get(self.url).content)

        self.assertFalse(report["degraded"])

    def test_a_cache_outage_is_degraded_but_not_a_readiness_failure_of_its_own(self) -> None:
        """`degraded` is the field that distinguishes serving-badly from down.

        Whether an unreachable cache should fail readiness outright is a separate
        question this does not change - the existing test asserting 503 still
        holds. This asserts only that the flag is set, so an operator reading the
        body can tell the two apart.
        """
        from urbanlens.dashboard.controllers.health import HealthController

        self.assertTrue(
            HealthController._is_degraded(cache_status="error", db_status="ok", connections={"used": 1, "max": 100}),
        )

    def test_unreadable_connection_stats_are_not_a_failure(self) -> None:
        """`pg_stat_activity` needs a privilege a hardened deployment may not grant.

        Reporting None there has to mean "not measured", never "degraded" - a
        probe that fails closed on a missing read privilege takes the site down
        for a permissions choice.
        """
        from urbanlens.dashboard.controllers.health import HealthController

        self.assertFalse(HealthController._is_degraded(cache_status="ok", db_status="ok", connections=None))

        with mock.patch.object(HealthController, "_probe_connections", return_value=None):
            response = Client().get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content)["connections"])

    def test_the_probe_survives_a_database_error(self) -> None:
        """It runs its own query, so it needs its own failure path."""
        from urbanlens.dashboard.controllers.health import HealthController

        with mock.patch("urbanlens.dashboard.controllers.health.connection") as fake:
            fake.vendor = "postgresql"
            fake.cursor.side_effect = DatabaseError("gone")
            self.assertIsNone(HealthController._probe_connections())
