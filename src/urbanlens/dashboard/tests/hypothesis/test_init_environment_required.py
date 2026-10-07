"""``bin/init.py`` starts every container, and refuses to guess which environment it is (Jess, 2026-10-07).

It used to fall back to production when ``UL_ENVIRONMENT`` was unset, and ``--environment`` reached init.py alone: the
``manage.py`` children it runs read the variable themselves, so they could disagree with it.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
from typing import TYPE_CHECKING, ClassVar
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes

if TYPE_CHECKING:
    from types import ModuleType

_INIT_PATH = pathlib.Path(__file__).resolve().parents[5] / "src" / "bin" / "init.py"


def _load_init_module() -> ModuleType:
    """Import ``src/bin/init.py``, which is a script rather than a package member.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_init_environment", _INIT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_INIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ResolveEnvironmentTests(SimpleTestCase):
    init_module: ClassVar[ModuleType]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.init_module = _load_init_module()

    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = pathlib.Path(directory.name) / ".env"

    def _resolve(self, explicit: str | None = None, **environ: str) -> str:
        return self.init_module.resolve_environment(explicit, environ=environ, env_file=self.env_file)

    def test_the_names_are_the_settings_names(self) -> None:
        self.assertEqual(sorted(self.init_module.ENVIRONMENTS), sorted(str(name) for name in EnvironmentTypes))

    def test_unset_or_blank_refuses_naming_the_variable_and_every_choice(self) -> None:
        for environ in ({}, {"UL_ENVIRONMENT": ""}, {"UL_ENVIRONMENT": "  "}):
            with self.subTest(environ=environ), self.assertRaises(self.init_module.UnrecoverableError) as caught:
                self._resolve(**environ)
            message = str(caught.exception)
            self.assertIn("UL_ENVIRONMENT is not set", message)
            for name in self.init_module.ENVIRONMENTS:
                self.assertIn(name, message)

    def test_the_variable_is_read_and_normalised(self) -> None:
        self.assertEqual(self._resolve(UL_ENVIRONMENT=" Staging\n"), "staging")

    def test_the_flag_wins_over_the_variable(self) -> None:
        self.assertEqual(self._resolve("development", UL_ENVIRONMENT="production"), "development")

    def test_a_checkout_s_env_file_is_read_as_django_reads_it(self) -> None:
        """``settings.base`` loads ``.env`` without overriding the process, so init.py agrees with its children."""
        self.env_file.write_text("UL_ENVIRONMENT=development\n", encoding="utf-8")
        self.assertEqual(self._resolve(), "development")
        self.assertEqual(self._resolve(UL_ENVIRONMENT="staging"), "staging")
        with self.assertRaises(self.init_module.UnrecoverableError):
            # Set but blank in the process: load_dotenv leaves it blank, and Django refuses, so this does too.
            self._resolve(UL_ENVIRONMENT="")

    def test_an_unknown_name_is_refused(self) -> None:
        with self.assertRaises(self.init_module.UnrecoverableError):
            self.init_module.DjangoProjectInitializer(environment="prodution")


class TheChildrenAgreeTests(SimpleTestCase):
    init_module: ClassVar[ModuleType]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.init_module = _load_init_module()

    def test_the_resolved_environment_is_exported_for_manage_py(self) -> None:
        for explicit, environ in (
            (None, {"UL_ENVIRONMENT": "Staging"}),
            ("development", {"UL_ENVIRONMENT": "production"}),
            ("local", {}),
        ):
            with self.subTest(explicit=explicit, environ=environ), mock.patch.dict(os.environ, environ, clear=True):
                initializer = self.init_module.DjangoProjectInitializer(environment=explicit)
                self.assertEqual(os.environ["UL_ENVIRONMENT"], initializer.environment)

    def test_an_unset_start_refuses_before_anything_runs(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(self.init_module, "ROOT_DIR", pathlib.Path(tempfile.gettempdir()) / "no-such-checkout"),
            self.assertRaisesRegex(self.init_module.UnrecoverableError, "UL_ENVIRONMENT is not set"),
        ):
            self.init_module.DjangoProjectInitializer()
