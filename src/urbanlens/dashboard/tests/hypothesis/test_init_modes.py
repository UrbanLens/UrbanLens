"""Only db-setup, which holds the owner's credentials, touches the schema; the app container only serves."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import TYPE_CHECKING, ClassVar
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_INIT_PATH = _REPO_ROOT / "src" / "bin" / "init.py"

_MIGRATE = "python src/urbanlens/manage.py migrate"
_APPLY_ROLES = "python src/urbanlens/manage.py apply_database_roles"


def _load_init_module() -> ModuleType:
    """Import ``src/bin/init.py``, which is a script rather than a package member.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_init_modes", _INIT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_INIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class InitModeTests(SimpleTestCase):
    init_module: ClassVar[ModuleType]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.init_module = _load_init_module()

    def _commands(self, mode) -> list[str]:
        """The commands one mode runs, with every subprocess and filesystem effect stubbed out."""
        initializer = object.__new__(self.init_module.DjangoProjectInitializer)
        initializer._environment = "production"
        initializer._db_host = "db"
        initializer._db_port = 5432
        initializer._db_name = "urbanlens"
        initializer._db_user = "postgres"
        initializer.no_runserver = False
        initializer.skip_frontend_build = False
        with (
            mock.patch.object(initializer, "run_command") as run_command,
            mock.patch.object(initializer, "check_db", return_value=True),
            mock.patch.object(initializer, "create_pgpass"),
            mock.patch.object(initializer, "copy_sample_env"),
            mock.patch.object(initializer, "verify_static_manifest"),
            mock.patch.object(pathlib.Path, "mkdir"),
        ):
            initializer.initialize_project(mode)
        return [" ".join(call.args[0]) for call in run_command.call_args_list]

    def test_db_only_migrates_then_applies_the_roles_and_serves_nothing(self) -> None:
        commands = self._commands(self.init_module.Mode.DATABASE)
        self.assertLess(
            commands.index(_MIGRATE),
            commands.index(_APPLY_ROLES),
            "the roles are granted the tables migrations create, so they have to come second",
        )
        self.assertFalse(
            [command for command in commands if "collectstatic" in command or command.startswith("bun")], commands
        )

    def test_no_db_serves_without_touching_the_schema(self) -> None:
        """The app logs in as ul_web, which can neither create a database nor migrate one."""
        commands = self._commands(self.init_module.Mode.SERVE)
        self.assertIn("bun run start", commands)
        self.assertFalse(
            [
                command
                for command in commands
                if command.startswith("psql") or _MIGRATE in command or _APPLY_ROLES in command
            ],
            commands,
        )

    def test_frontend_only_touches_no_database(self) -> None:
        """The image build runs it, with no database to reach."""
        commands = self._commands(self.init_module.Mode.FRONTEND)
        self.assertTrue([command for command in commands if "collectstatic" in command], commands)
        self.assertFalse(
            [
                command
                for command in commands
                if command.startswith("psql") or "manage.py migrate" in command or command == "bun run start"
            ],
            commands,
        )
