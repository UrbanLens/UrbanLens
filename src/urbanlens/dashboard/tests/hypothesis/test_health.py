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
