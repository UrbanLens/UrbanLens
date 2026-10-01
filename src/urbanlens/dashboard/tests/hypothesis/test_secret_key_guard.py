"""A missing DJANGO_SECRET_KEY outside 'local' fails at startup, not at read time."""

from __future__ import annotations

import os
import subprocess
import sys

from urbanlens.core.tests.testcase import SimpleTestCase

_IMPORT_SETTINGS = "import urbanlens.UrbanLens.settings.base"


class SecretKeyGuardTests(SimpleTestCase):
    def _boot(self, environment: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "UL_ENVIRONMENT": environment, "DJANGO_SECRET_KEY": ""}
        return subprocess.run(
            [sys.executable, "-c", _IMPORT_SETTINGS],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )

    def test_production_without_a_secret_key_refuses_to_boot(self) -> None:
        result = self._boot("production")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_development_without_a_secret_key_still_boots(self) -> None:
        """Development is exempt, deliberately - staging and production are not.

        The merged guard draws the line at durable data instead: ephemeral keys are safe where no encrypted data
        needs to survive a restart, which covers developer machines and test runs."""
        result = self._boot("development")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_without_a_secret_key_still_boots(self) -> None:
        result = self._boot("local")
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
