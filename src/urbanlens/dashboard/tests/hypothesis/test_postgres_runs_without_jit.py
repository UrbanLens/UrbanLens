"""What the ``db`` service tells Postgres about itself, and why each of those is not the default.

JIT is on by default and compiles any statement the planner costs above 100,000, which one heavy account's queries
reach: the nearby-pins statement for a 17,720-pin account spent 150 ms of its 204 ms compiling, and ran in 98 ms
without it. A web request's statements are short; the compilation never pays for itself.

The rest is server sizing. The image's defaults describe a machine nobody here runs: a 128 MB buffer cache against a
1,000 MB database, a seek priced at four sequential reads, and a 4 GB page-cache estimate against a 2 GB container.
X27 measured what that costs and why ``pg_stat_statements`` has to be present to measure it again.
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


def _default_of(value: str) -> str:
    """The ``${VAR:-default}`` fallback compose would use where the variable is unset."""
    _, _, default = value.partition(":-")
    return default.rstrip("}") or value


class PostgresIsSizedForItsContainerTests(SimpleTestCase):
    """Stock Postgres assumes it has a machine to itself; this one has a 2 GB cgroup and an SSD."""

    def setUp(self) -> None:
        super().setUp()
        self.settings_given = _db_settings()

    def test_the_buffer_cache_is_not_the_image_default(self) -> None:
        """128MB is smaller than the database it is caching."""
        self.assertIn("shared_buffers", self.settings_given)
        self.assertNotEqual(_default_of(self.settings_given["shared_buffers"]), "128MB")

    def test_the_page_cache_estimate_is_sized_to_the_container(self) -> None:
        """The image autodetects the host's memory, and the host is not what this service may use."""
        self.assertIn("effective_cache_size", self.settings_given)
        self.assertNotEqual(_default_of(self.settings_given["effective_cache_size"]), "4GB")

    def test_a_seek_is_not_priced_as_a_spinning_disk(self) -> None:
        self.assertLess(float(_default_of(self.settings_given.get("random_page_cost", "4"))), 2.0)

    def test_statement_statistics_are_available_without_an_alter_system(self) -> None:
        """X27's figures came from a setting someone applied by hand to one container, which is
        indistinguishable from a measurement nobody can repeat."""
        self.assertEqual(self.settings_given.get("shared_preload_libraries"), "pg_stat_statements")

    def test_planning_time_tracking_is_off_unless_a_deployment_asks(self) -> None:
        """Anti-vacuity for the line above: it is Postgres's own default, its cost here was not
        measurable against host noise, and X27 needs it turned on deliberately rather than always."""
        self.assertEqual(_default_of(self.settings_given["pg_stat_statements.track_planning"]), "off")

    def test_every_setting_is_overridable_without_editing_compose(self) -> None:
        """A deployment with a different memory limit has to be able to say so."""
        for name in ("shared_buffers", "effective_cache_size", "random_page_cost", "pg_stat_statements.track_planning"):
            with self.subTest(setting=name):
                self.assertTrue(self.settings_given[name].startswith("${"), f"{name} is not env-driven")
