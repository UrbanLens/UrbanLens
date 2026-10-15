"""The restore and verify scripts default to the app container compose creates, not a developer's."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[5]
LIBRARY = REPO_ROOT / "bin" / "lib" / "compose_names.sh"


def _name(env_file: str | None, **env: str) -> str:
    """The container name the library resolves for a project directory holding ``env_file`` as its .env."""
    with tempfile.TemporaryDirectory() as root:
        if env_file is not None:
            (Path(root) / ".env").write_text(env_file, encoding="utf-8")
        clean = {
            k: v for k, v in os.environ.items() if k not in {"UL_APP_CONTAINER", "UL_CONTAINER_NAME", "UL_ENVIRONMENT"}
        }
        result = subprocess.run(  # noqa: S603
            ["bash", "-c", f'. "{LIBRARY}"; ul_app_container_name "$1"', "bash", root],  # noqa: S607
            env={**clean, **env},
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()


class AppContainerNameTests(SimpleTestCase):
    def test_no_env_means_the_production_name_compose_uses(self) -> None:
        self.assertEqual(_name(None), "urbanlens_production_app")

    def test_the_container_name_in_env_wins_over_the_environment(self) -> None:
        self.assertEqual(_name("UL_ENVIRONMENT=staging\nUL_CONTAINER_NAME=mine\n"), "urbanlens_mine_app")

    def test_the_environment_in_env_is_used_without_a_container_name(self) -> None:
        self.assertEqual(_name("UL_ENVIRONMENT=staging\nUL_CONTAINER_NAME=\n"), "urbanlens_staging_app")

    def test_quoted_values_and_comments_are_read_like_compose_does(self) -> None:
        self.assertEqual(_name('#UL_CONTAINER_NAME=no\nUL_CONTAINER_NAME="quoted"\n'), "urbanlens_quoted_app")

    def test_the_shell_beats_env_as_it_does_in_compose(self) -> None:
        self.assertEqual(_name("UL_CONTAINER_NAME=file\n", UL_CONTAINER_NAME="shell"), "urbanlens_shell_app")

    def test_an_explicit_container_wins(self) -> None:
        self.assertEqual(_name("UL_CONTAINER_NAME=file\n", UL_APP_CONTAINER="exact"), "exact")

    def test_both_scripts_use_the_library(self) -> None:
        for script in ("restore_backup.sh", "verify_backup_restore.sh"):
            with self.subTest(script=script):
                text = (REPO_ROOT / "bin" / script).read_text(encoding="utf-8")
                self.assertIn("ul_app_container_name", text)
                self.assertNotIn("urbanlens_development_main_app", text)
