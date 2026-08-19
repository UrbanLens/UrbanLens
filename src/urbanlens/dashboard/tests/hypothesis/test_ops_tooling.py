"""The host-side ops tooling: staging pipeline, dev environments, dev router.

These tools decide whether production data gets copied over a database and
whether an environment's URL points at the right stack, so the parts worth
pinning are the refusals and the boundaries - not the happy paths, which are
proven by running them.

They live in ``bin/opslib`` and are stdlib-only by design (they must work on a
host where the project venv does not exist), so this reaches them by path
rather than by package import.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase


def _opslib_dir() -> Path:
    """Locate ``bin/`` by walking up, rather than counting parents.

    Counting is how the first version of this got it wrong - and a wrong count
    does not fail loudly, it silently points at a directory that does not
    exist. Searching for the thing itself cannot be off by one.

    Returns:
        The directory containing the ``opslib`` package.

    Raises:
        RuntimeError: ``bin/opslib`` is nowhere above this file.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "bin"
        if (candidate / "opslib" / "__init__.py").is_file():
            return candidate
    raise RuntimeError("bin/opslib not found above this test file")


_BIN = _opslib_dir()
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from opslib import devenv, router  # noqa: E402 - path setup must precede the import
from opslib.staging import _read_env_var, _table_counts  # noqa: E402


class EnvFileReadingTests(SimpleTestCase):
    """Reading .env without sourcing it, since sourcing a deploy target's env is a way to run its contents."""

    def _env(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write(text)
        handle.close()
        return Path(handle.name)

    def test_a_plain_value_is_read(self) -> None:
        self.assertEqual(_read_env_var(self._env("UL_APP_PORT=21800\n"), "UL_APP_PORT"), "21800")

    def test_quotes_are_stripped(self) -> None:
        self.assertEqual(_read_env_var(self._env('UL_SITE_URL="https://x.test"\n'), "UL_SITE_URL"), "https://x.test")

    def test_a_missing_key_falls_back(self) -> None:
        self.assertEqual(_read_env_var(self._env("A=1\n"), "B", "fallback"), "fallback")

    def test_a_missing_file_falls_back(self) -> None:
        self.assertEqual(_read_env_var(Path("/nonexistent/.env"), "A", "fallback"), "fallback")

    def test_a_prefix_match_is_not_a_match(self) -> None:
        """UL_DB_NAME must not be answered by UL_DB_NAME_SUFFIX."""
        self.assertEqual(_read_env_var(self._env("UL_DB_NAME_SUFFIX=x\nUL_DB_NAME=real\n"), "UL_DB_NAME"), "real")


class TableCountGuardTests(SimpleTestCase):
    """psql has no bind parameter for an identifier, so the guard is the safety."""

    def test_a_non_identifier_is_refused_without_running_anything(self) -> None:
        counts = _table_counts("no-such-container", "u", "d", ("users; DROP TABLE x",))

        self.assertEqual(counts["users; DROP TABLE x"], -1)

    def test_an_unreachable_container_reports_unknown_rather_than_raising(self) -> None:
        """A verification step must not take down the deploy report it belongs to."""
        counts = _table_counts("no-such-container", "u", "d", ("auth_user",))

        self.assertEqual(counts["auth_user"], -1)


class SlugAllocationTests(SimpleTestCase):
    def test_generated_slugs_are_unique_and_dns_safe(self) -> None:
        entries: dict[str, dict] = {}
        first = devenv.generate_slug(entries)
        entries[first] = {}
        second = devenv.generate_slug(entries)

        self.assertNotEqual(first, second)
        for slug in (first, second):
            self.assertRegex(slug, r"^[a-z0-9-]+$")

    def test_a_requested_name_is_sanitised(self) -> None:
        self.assertEqual(devenv.generate_slug({}, "Floor Plans!!"), "floor-plans")

    def test_a_taken_name_is_refused_rather_than_silently_reused(self) -> None:
        """Reusing one would point two environments at one set of containers."""
        with self.assertRaises(ValueError):
            devenv.generate_slug({"taken": {}}, "taken")

    def test_a_name_with_nothing_usable_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            devenv.generate_slug({}, "!!!")


class PortAllocationTests(SimpleTestCase):
    def test_a_registered_port_is_skipped(self) -> None:
        """The registry records intent: a stopped environment still owns its ports."""
        base = devenv.allocate_ports({"x": {"app_port": devenv.PORT_BASE}})

        self.assertNotEqual(base, devenv.PORT_BASE)

    def test_allocation_lands_on_a_block_boundary(self) -> None:
        base = devenv.allocate_ports({})

        self.assertEqual((base - devenv.PORT_BASE) % devenv.PORT_STRIDE, 0)


class EnvFileWritingTests(SimpleTestCase):
    def test_secrets_are_inherited_and_overrides_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.env"
            source.write_text("UL_STRIPE_SECRET_KEY=sk_test\nUL_APP_PORT=21800\n", encoding="utf-8")
            destination = Path(directory) / "out.env"

            devenv._write_env_file(destination, {"UL_APP_PORT": "31000"}, source)

            written = destination.read_text(encoding="utf-8")
            self.assertIn("UL_STRIPE_SECRET_KEY=sk_test", written)
            self.assertIn("UL_APP_PORT=31000", written)
            self.assertNotIn("UL_APP_PORT=21800", written)


class RouterConfigTests(SimpleTestCase):
    def test_each_environment_gets_its_own_server_block(self) -> None:
        config = router.render_config({
            "a": {"hostname": "a.dev.example", "app_port": 31000},
            "b": {"hostname": "b.dev.example", "app_port": 31020},
        })

        self.assertIn("a.dev.example", config)
        self.assertIn("31000", config)
        self.assertIn("b.dev.example", config)
        self.assertIn("31020", config)

    def test_an_unmatched_host_gets_a_404_not_someone_elses_environment(self) -> None:
        """Without a default server, nginx serves the first block to any unmatched host -
        so a destroyed environment's URL would quietly show a live one."""
        config = router.render_config({"a": {"hostname": "a.dev.example", "app_port": 31000}})

        self.assertIn("default_server", config)
        self.assertIn("No such dev environment", config)

    def test_an_incomplete_entry_is_skipped_rather_than_rendered_broken(self) -> None:
        config = router.render_config({"broken": {"hostname": "", "app_port": None}})

        self.assertNotIn("proxy_pass http://host.docker.internal:None", config)


class ContainerNameCollisionTests(SimpleTestCase):
    """The guard whose absence let a create recreate the host's dev stack.

    Container names are global to a docker host, so two environments that
    resolve the same names fight over the same containers - and `docker compose
    up` reports that as a successful build while somebody else's environment
    stops working. Isolation therefore cannot depend on a variable being
    present in the branch being deployed: `UL_CONTAINER_NAME` exists on some
    branches and not others, and where it is absent the names fall back to
    `UL_ENVIRONMENT`, which every dev environment sets to `development` to get
    the autoreloader.
    """

    def test_a_name_owned_by_another_directory_is_refused(self) -> None:
        listing = "ul_a_db\t/envs/a/UrbanLens\nurbanlens_development_app\t/projects/UrbanLens/UrbanLens\n"

        conflict = devenv._conflicting_name({"urbanlens_development_app"}, listing, Path("/envs/mine/UrbanLens"))

        self.assertIn("urbanlens_development_app", conflict)
        self.assertIn("/projects/UrbanLens/UrbanLens", conflict, "the owner is named so the report says whose stack it is")

    def test_our_own_containers_are_not_a_conflict(self) -> None:
        """Recreating our own stack is what `create` on an existing slug does."""
        listing = "ul_mine_db\t/envs/mine/UrbanLens\n"

        self.assertEqual(devenv._conflicting_name({"ul_mine_db"}, listing, Path("/envs/mine/UrbanLens")), "")

    def test_an_unrelated_container_is_ignored(self) -> None:
        listing = "some_other_thing\t/elsewhere\n"

        self.assertEqual(devenv._conflicting_name({"ul_mine_db"}, listing, Path("/envs/mine/UrbanLens")), "")

    def test_a_container_with_no_recorded_owner_still_blocks(self) -> None:
        """Docker container names are global, so `up` fails on a duplicate whatever
        started it. This host already carries `ul_*` containers from another tool
        with no compose labels at all."""
        listing = "ul_mine_db\t\n"

        conflict = devenv._conflicting_name({"ul_mine_db"}, listing, Path("/envs/mine/UrbanLens"))

        self.assertIn("unknown owner", conflict)


class IsolationOverrideTests(SimpleTestCase):
    def test_every_named_service_is_pinned_to_the_slug(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "docker-compose.yml").write_text("services:\n  app:\n    image: x\n  db:\n    image: y\n", encoding="utf-8")

            override = devenv._isolation_override("abc123", checkout)

            self.assertIn("container_name: ul_abc123_app", override)
            self.assertIn("container_name: ul_abc123_db", override)

    def test_a_service_the_branch_does_not_define_is_not_invented(self) -> None:
        """Overriding a service the base file lacks makes compose reject the whole config."""
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "docker-compose.yml").write_text("services:\n  app:\n    image: x\n", encoding="utf-8")

            override = devenv._isolation_override("abc123", checkout)

            self.assertIn("ul_abc123_app", override)
            self.assertNotIn("clamav", override)
