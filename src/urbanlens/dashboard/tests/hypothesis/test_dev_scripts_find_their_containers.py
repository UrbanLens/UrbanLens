"""The developer scripts default to the containers compose names for this checkout's stack, not one fixed stack."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[5]
LIBRARY = REPO_ROOT / "bin" / "lib" / "compose_names.sh"
RESOLVING_SCRIPTS = ("run_tests.sh", "run_mutation_tests.sh", "run_contract_tests.sh", "sync_app.sh")
NAMED_IN_EXAMPLES = (
    "bin/perf/pyspy.sh",
    "bin/run_perf_tests.sh",
    "bin/report_unnamed_write_routes.py",
    "tests/perf/README.md",
)
FIXED_STACK = "urbanlens_development_main"


def _container(suffix: str, env_file: str | None, **env: str) -> str:
    """The container name the library resolves for a service suffix in a project holding ``env_file`` as .env."""
    with tempfile.TemporaryDirectory() as root:
        if env_file is not None:
            (Path(root) / ".env").write_text(env_file, encoding="utf-8")
        clean = {
            k: v for k, v in os.environ.items() if k not in {"UL_APP_CONTAINER", "UL_CONTAINER_NAME", "UL_ENVIRONMENT"}
        }
        result = subprocess.run(  # noqa: S603
            ["bash", "-c", f'. "{LIBRARY}"; ul_container_name "$1" "$2"', "bash", root, suffix],  # noqa: S607
            env={**clean, **env},
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()


class ContainerNameTests(SimpleTestCase):
    def test_no_env_means_the_production_stack(self) -> None:
        self.assertEqual(_container("test_runner", None), "urbanlens_production_test_runner")

    def test_the_container_name_in_env_names_the_service(self) -> None:
        self.assertEqual(
            _container("test_runner", "UL_CONTAINER_NAME=development_main\n"), f"{FIXED_STACK}_test_runner"
        )

    def test_the_environment_stands_in_without_a_container_name(self) -> None:
        self.assertEqual(_container("db", "UL_ENVIRONMENT=staging\n"), "urbanlens_staging_db")

    def test_the_shell_beats_env(self) -> None:
        self.assertEqual(
            _container("app", "UL_CONTAINER_NAME=file\n", UL_CONTAINER_NAME="shell"), "urbanlens_shell_app"
        )


class DevScriptTests(SimpleTestCase):
    def test_the_scripts_that_start_containers_resolve_them_through_the_library(self) -> None:
        for script in RESOLVING_SCRIPTS:
            with self.subTest(script=script):
                text = (REPO_ROOT / "bin" / script).read_text(encoding="utf-8")
                self.assertIn("compose_names.sh", text)
                self.assertNotIn(FIXED_STACK, text)

    def test_the_examples_name_the_containers_generically(self) -> None:
        for name in NAMED_IN_EXAMPLES:
            with self.subTest(name=name):
                self.assertNotIn(FIXED_STACK, (REPO_ROOT / name).read_text(encoding="utf-8"))
