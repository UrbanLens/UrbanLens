"""Startup must not depend on writing a `.env` into the image.

On 2026-08-17 staging would not start. `.env*` had just been excluded from the
image - correctly, because a secret baked into a layer outlives its own rotation
- and `bin/init.py` responded by trying to *create* `/app/.env` from
`.env-sample`. The app directory is not writable by the container's user, so the
write raised `PermissionError`, the initializer turned that into
`UnrecoverableError`, and the container never became healthy. Every service that
waits on it failed with it.

The path had existed for a long time and never run: while `.env` was baked into
the image, `env_file.exists()` returned early. Removing the secret exposed a
latent fatal branch, which is the shape worth testing for - not the secret, and
not the permission.

Two properties, and they are separate:

- A deployed environment never tries to write the file at all. It is configured
  from real environment variables (compose's ``env_file:``), so there is nothing
  to seed, and writing `.env-sample`'s placeholders beside real values would at
  best be noise.
- A failure to write is never fatal anywhere. The file is a convenience for
  someone running from a checkout. A genuine misconfiguration still fails
  loudly, with a far better message, at the ``DJANGO_SECRET_KEY`` guard.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase

_INIT_PATH = pathlib.Path(__file__).resolve().parents[4] / "bin" / "init.py"


def _load_init_module():
    """Import ``src/bin/init.py``, which is a script rather than a package member.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_init", _INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CopySampleEnvTests(SimpleTestCase):
    """``copy_sample_env`` must never be the reason a deployment fails to start."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.init_module = _load_init_module()

    def _initializer(self, environment: str):
        """An initializer for *environment*, without running its constructor's setup."""
        initializer = object.__new__(self.init_module.DjangoProjectInitializer)
        initializer._environment = environment
        return initializer

    def _run_with_missing_env(self, environment: str, tmp_root: pathlib.Path):
        """Run copy_sample_env against a root holding a sample but no `.env`."""
        (tmp_root / ".env-sample").write_text("UL_EXAMPLE=1\n", encoding="utf-8")
        with mock.patch.object(self.init_module, "ROOT_DIR", tmp_root):
            self._initializer(environment).copy_sample_env()

    def test_staging_does_not_write_an_env_file(self) -> None:
        """The case that broke staging: nothing to seed, so nothing is attempted."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            self._run_with_missing_env("staging", root)

            self.assertFalse((root / ".env").exists(), "a deployed environment must not seed .env - it is configured from the environment")

    def test_production_does_not_write_an_env_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            self._run_with_missing_env("production", root)

            self.assertFalse((root / ".env").exists())

    def test_a_local_checkout_is_still_seeded(self) -> None:
        """The convenience this exists for must keep working."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            self._run_with_missing_env("local", root)

            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "UL_EXAMPLE=1\n")

    def test_an_unwritable_directory_is_not_fatal(self) -> None:
        """A read-only app directory must not abort startup, in any environment."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            (root / ".env-sample").write_text("UL_EXAMPLE=1\n", encoding="utf-8")

            with mock.patch.object(self.init_module, "ROOT_DIR", root), mock.patch("pathlib.Path.open", side_effect=PermissionError(13, "Permission denied")):
                # Must return rather than raise: this is the exact failure staging hit.
                self._initializer("local").copy_sample_env()

    def test_an_existing_env_file_is_left_alone(self) -> None:
        """Never overwrite real configuration with the sample."""
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            (root / ".env-sample").write_text("UL_EXAMPLE=1\n", encoding="utf-8")
            (root / ".env").write_text("UL_REAL=secret\n", encoding="utf-8")

            with mock.patch.object(self.init_module, "ROOT_DIR", root):
                self._initializer("local").copy_sample_env()

            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "UL_REAL=secret\n")
