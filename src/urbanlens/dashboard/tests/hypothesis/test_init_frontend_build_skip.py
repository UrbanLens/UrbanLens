"""The frontend build must be skippable, because bind-mounting makes it fatal."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest import mock

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_INIT_PATH = _REPO_ROOT / "src" / "bin" / "init.py"


def _load_init_module():
    """Import ``src/bin/init.py``, which is a script rather than a package member.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_init_frontend", _INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FrontendBuildSkipTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.init_module = _load_init_module()

    def _initializer(self, *, skip: bool):
        """An initializer with the flag resolved, without running the constructor."""
        initializer = object.__new__(self.init_module.DjangoProjectInitializer)
        initializer._environment = "development"
        initializer.skip_frontend_build = skip
        return initializer

    def test_the_flag_stops_it_before_it_touches_the_filesystem(self) -> None:
        initializer = self._initializer(skip=True)
        with (
            mock.patch.object(initializer, "run_command") as run_command,
            mock.patch.object(pathlib.Path, "mkdir") as mkdir,
        ):
            initializer.build_frontend()
        run_command.assert_not_called()
        mkdir.assert_not_called()

    def test_without_the_flag_it_still_builds(self) -> None:
        """The skip must be opt-in: every other deployment needs this to run."""
        initializer = self._initializer(skip=False)
        with mock.patch.object(initializer, "run_command") as run_command, mock.patch.object(pathlib.Path, "mkdir"):
            initializer.build_frontend()
        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertTrue(any("sass" in " ".join(command) for command in commands), commands)
        self.assertTrue(any("collectstatic" in " ".join(command) for command in commands), commands)

    def test_the_hot_reload_overlay_sets_it(self) -> None:
        """A flag nothing sets is a flag that fixes nothing."""
        overlay = yaml.safe_load((_REPO_ROOT / "docker-compose.hot-reload.yml").read_text(encoding="utf-8"))
        environment = overlay["services"]["app"]["environment"]
        self.assertIn("UL_SKIP_FRONTEND_BUILD", environment)
        self.assertIn(str(environment["UL_SKIP_FRONTEND_BUILD"]).lower(), {"1", "true", "yes"})

    def test_the_overlay_still_bind_mounts_the_tree_this_is_about(self) -> None:
        """If the mount goes away, so does the reason for the flag."""
        overlay = yaml.safe_load((_REPO_ROOT / "docker-compose.hot-reload.yml").read_text(encoding="utf-8"))
        self.assertIn("./src:/app/src", overlay["services"]["app"]["volumes"])
