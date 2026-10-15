"""``UL_LOG_DIR``: unset keeps the file log, blank turns it off (the container default), a path moves it."""

from __future__ import annotations

import importlib
from logging.config import dictConfig
import os
from pathlib import Path
import tempfile
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
import urbanlens.UrbanLens.settings.base as settings_base

REPO_ROOT = Path(__file__).resolve().parents[5]


class LogDirTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(importlib.reload, settings_base)

    def _reload(self, log_dir: str | None):
        env = {"UL_ENVIRONMENT": "development"}
        if log_dir is not None:
            env["UL_LOG_DIR"] = log_dir
        with (
            mock.patch.dict("os.environ", env),
            mock.patch("sys.argv", ["daphne", "urbanlens.UrbanLens.asgi:application"]),
        ):
            if log_dir is None:
                os.environ.pop("UL_LOG_DIR", None)
            return importlib.reload(settings_base)

    def test_blank_means_console_only_and_the_config_still_loads(self) -> None:
        module = self._reload("")

        self.assertEqual(module.LOGGING["root"]["handlers"], ["console"])
        self.assertNotIn("file", module.LOGGING["handlers"])
        dictConfig(module.LOGGING)

    def test_a_directory_adds_the_file_handler_there(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            module = self._reload(directory)

            self.assertIn("file", module.LOGGING["root"]["handlers"])
            self.assertEqual(module.LOGGING["handlers"]["file"]["filename"], str(Path(directory) / "django.log"))

    def test_unset_keeps_the_project_logs_directory(self) -> None:
        module = self._reload(None)

        self.assertEqual(Path(module.LOG_DIR).name, "logs")

    def test_the_image_defaults_to_no_file_log(self) -> None:
        self.assertRegex((REPO_ROOT / "Dockerfile").read_text(encoding="utf-8"), r"(?m)^\s+UL_LOG_DIR=\s*$")
