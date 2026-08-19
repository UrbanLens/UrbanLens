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
