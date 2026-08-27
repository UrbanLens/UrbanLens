"""A missing DJANGO_SECRET_KEY outside 'local' fails at startup, not at read time.

Without the guard, ``base.py`` fell back to ``get_random_secret_key()`` *per
process*: sessions and CSRF break across workers, and ``EncryptedTextField``
(which derives its key from ``SECRET_KEY``) silently writes rows no other
process - or any restart - can ever decrypt. See audit chunk 441/454.

Subprocess-based by necessity: this process's settings are already loaded, so
the import-time guard can only be exercised in a fresh interpreter.
``DJANGO_SECRET_KEY`` is set to an *empty string* rather than removed, because
``base.py`` runs ``load_dotenv`` (non-overriding) and a popped variable could
be silently re-filled from a ``.env`` on disk - empty survives it.
"""

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

        This asserted the opposite until the 2026-08-17 merge, on the reasoning
        that dev compose also runs several processes and so shares the
        per-process-key hazard. The merged guard draws the line at durable data
        instead: ephemeral keys are safe where no encrypted data needs to
        survive a restart, which covers developer machines and test runs.

        The tradeoff is real and worth knowing: a dev database *can* hold
        encrypted rows, and restarting without a key orphans them. That is an
        annoyance on a dev box and a catastrophe in staging, which is why only
        the latter fails hard.
        """
        result = self._boot("development")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_without_a_secret_key_still_boots(self) -> None:
        result = self._boot("local")
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
