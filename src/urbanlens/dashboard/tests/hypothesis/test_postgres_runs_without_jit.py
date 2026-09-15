"""Postgres compiles no query with JIT.

JIT is on by default and compiles any statement the planner costs above 100,000, which one heavy account's queries
reach: the nearby-pins statement for a 17,720-pin account spent 150 ms of its 204 ms compiling, and ran in 98 ms
without it. A web request's statements are short; the compilation never pays for itself.
"""

from __future__ import annotations

import itertools
import pathlib

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _db_command() -> list[str]:
    service = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())["services"]["db"]
    command = service.get("command") or []
    return command.split() if isinstance(command, str) else [str(part) for part in command]


def _db_settings() -> dict[str, str]:
    command = _db_command()
    return dict(value.partition("=")[::2] for flag, value in itertools.pairwise(command) if flag == "-c")


class PostgresRunsWithoutJitTests(SimpleTestCase):
    def test_the_database_service_turns_jit_off(self) -> None:
        self.assertEqual(_db_settings().get("jit"), "off")

    def test_the_database_service_still_starts_postgres(self) -> None:
        command = _db_command()
        self.assertTrue(command, "the image's own command runs postgres; an override has to as well")
        self.assertEqual(command[0], "postgres")
