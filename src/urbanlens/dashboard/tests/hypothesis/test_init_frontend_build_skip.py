"""The frontend build must be skippable, because bind-mounting makes it fatal.

`docker-compose.hot-reload.yml` mounts the checkout over `/app/src` so an edit is
live without a rebuild. That makes the build's output directories the *host*
user's, and `build_frontend` opens by creating them — so unless the host uid
happens to equal the container's, `mkdir` raises `PermissionError`, the
initializer turns it into `UnrecoverableError`, and the container crash-loops
before serving anything. The overlay already redirects the log directory out of
the mounted tree for exactly this reason; the build needed the same treatment.

The build is also redundant there: the overlay runs a `sass-watch` sidecar, and
`UL_ENVIRONMENT: development` leaves `DEBUG` on, where staticfiles serves from
the app directories rather than from a collected root.

Two halves, and the second is what makes the first mean anything: the flag has
to be read, and the overlay has to set it.
"""

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
