"""Health endpoint and ALLOWED_HOSTS defaults used by Docker healthchecks."""

from __future__ import annotations

import os
from unittest import mock

from django.test import Client, override_settings

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.UrbanLens.settings.app import _default_allowed_hosts


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
