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

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

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
from opslib.staging import _VERIFIED_TABLES, _db_container, _read_env_var, _staging_base_url, _table_counts  # noqa: E402


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


def _services_with_extra_hosts(override: str) -> set[str]:
    """Which services in a generated override declare ``extra_hosts``.

    Parsed rather than sliced between service names: the override emits
    services in its own fixed order, not the order the base compose file lists
    them, so splitting on "the text between app and celery-worker" reads a
    different block than it looks like it does.
    """
    found: set[str] = set()
    current = ""
    for line in override.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
        elif line.strip() == "extra_hosts:":
            found.add(current)
    return found


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

    def test_application_services_can_reach_the_host(self) -> None:
        """Where this environment's own REData actually is.

        REData publishes its app port on the host; ``127.0.0.1`` inside the app
        container is the app container, so the configured URL resolved to
        nothing. The failure is close to invisible - the same URL works from a
        shell on the host, which is where anyone would test it.
        """
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "docker-compose.yml").write_text("services:\n  app:\n    image: x\n  celery-worker:\n    image: y\n  db:\n    image: z\n", encoding="utf-8")

            override = devenv._isolation_override("abc123", checkout)

        routed = _services_with_extra_hosts(override)

        self.assertEqual(routed, {"app", "celery-worker"}, "only services running application code need a route out")

    def test_the_redata_url_names_the_alias_the_override_routes(self) -> None:
        """The env file and the override are written separately and must agree."""
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "docker-compose.yml").write_text("services:\n  app:\n    image: x\n", encoding="utf-8")
            override = devenv._isolation_override("abc123", checkout)

        url = devenv.redata_api_url(21860)

        self.assertNotIn("127.0.0.1", url)
        self.assertIn(url.split("//", 1)[1].split(":", 1)[0], override)

    def test_a_minted_key_replaces_the_inherited_one(self) -> None:
        """The seeded key names a row in the *host's* REData database.

        A private instance starts with an empty one, so the inherited key
        authenticates against nothing and every call is a 401 - from a service
        that is up, reachable, and answering.
        """
        with tempfile.TemporaryDirectory() as directory:
            ul_dir = Path(directory)
            (ul_dir / ".env").write_text("UL_REDATA_API_KEY=rdk_inherited\nUL_SITE_URL=http://x\n", encoding="utf-8")
            run = mock.Mock()
            with mock.patch.object(devenv.subprocess, "run", return_value=mock.Mock(stdout="90 objects imported\nUL_REDATA_KEY=rdk_fresh\n", stderr="")):
                devenv._provision_redata_key(run, "abc123", ul_dir)

            written = (ul_dir / ".env").read_text(encoding="utf-8")

        self.assertIn("UL_REDATA_API_KEY=rdk_fresh", written)
        self.assertNotIn("rdk_inherited", written)
        self.assertIn("UL_SITE_URL=http://x", written, "the rest of the file must survive")
        self.assertEqual(run.record.call_args.args[1], "ok")

    def test_a_failed_mint_is_recorded_rather_than_raised(self) -> None:
        """Everything that is not REData still works, which is most of it."""
        with tempfile.TemporaryDirectory() as directory:
            ul_dir = Path(directory)
            (ul_dir / ".env").write_text("UL_REDATA_API_KEY=rdk_inherited\n", encoding="utf-8")
            run = mock.Mock()
            with mock.patch.object(devenv.subprocess, "run", return_value=mock.Mock(stdout="", stderr="no such container")):
                devenv._provision_redata_key(run, "abc123", ul_dir)

            self.assertEqual((ul_dir / ".env").read_text(encoding="utf-8"), "UL_REDATA_API_KEY=rdk_inherited\n")

        self.assertEqual(run.record.call_args.args[1], "failed")
        self.assertIn("no such container", run.record.call_args.args[2])

    def test_redata_answers_to_the_host_the_url_names(self) -> None:
        """Routing to it is only half the fix; Django still checks the Host header.

        Verified live 2026-08-20: over the host gateway REData answered 400
        DisallowedHost, where the same request from the host answered 401. A
        400 from a service that is plainly up reads as a bad request, not as a
        misconfigured address, so this half is the easier one to miss.
        """
        host = devenv.redata_api_url(21860).split("//", 1)[1].split(":", 1)[0]

        self.assertIn(host, devenv.redata_allowed_hosts().split(","))


class RedataModeTests(SimpleTestCase):
    """Which REData an environment talks to, and what that costs somebody else.

    REData exposes almost no write surfaces: most of its "write" operations do
    not store what we send, they make REData go *fetch* external data about a
    location. So a per-environment REData does not only cost eight containers
    and eight minutes - it spends third-party quota pulling data that is deleted
    along with the environment. Production answers most of the same calls from
    its cache without contacting anything, and caches whatever is genuinely new
    for good. That is why sharing is the default and a private instance is a
    deliberate flag, and why these mappings are worth pinning: getting the
    default wrong is not visible from inside the environment at all.
    """

    def test_the_three_modes_are_the_three_states(self) -> None:
        self.assertEqual(devenv.REDATA_MODES, ("production", "own", "none"))

    def test_creating_an_environment_shares_production_unless_told_otherwise(self) -> None:
        """The default is the whole point of the change; a signature default is how it is expressed."""
        import inspect

        self.assertEqual(inspect.signature(devenv.create).parameters["redata"].default, devenv.REDATA_PRODUCTION)

    def test_an_unknown_mode_is_refused_rather_than_guessed(self) -> None:
        """`create` reports rather than raises, so this has to be visible in the record."""
        with tempfile.TemporaryDirectory() as directory:
            record = devenv.create(redata="sometimes", root=Path(directory), run_dir=Path(directory) / "runs")

        self.assertEqual(record["status"], "failed")
        self.assertIn("unknown redata mode", record["steps"][0]["detail"])

    def test_only_a_private_instance_reserves_a_port(self) -> None:
        """Nothing should record an address for a stack that does not exist."""
        self.assertEqual(devenv.redata_port_for(devenv.REDATA_OWN, 31000), 31001)
        self.assertIsNone(devenv.redata_port_for(devenv.REDATA_PRODUCTION, 31000))
        self.assertIsNone(devenv.redata_port_for(devenv.REDATA_NONE, 31000))

    def test_production_writes_the_inherited_url_and_key(self) -> None:
        values = devenv.redata_settings(devenv.REDATA_PRODUCTION, None, ("https://redata.example.test", "rdk_production"))

        self.assertEqual(values["UL_REDATA_API_URL"], "https://redata.example.test")
        self.assertEqual(values["UL_REDATA_API_KEY"], "rdk_production")

    def test_a_private_instance_is_addressed_over_the_host_gateway(self) -> None:
        """Not 127.0.0.1, which inside the app container is the app container."""
        values = devenv.redata_settings(devenv.REDATA_OWN, 31001, ("https://redata.example.test", "rdk_production"))

        self.assertEqual(values["UL_REDATA_API_URL"], devenv.redata_api_url(31001))
        self.assertNotIn("127.0.0.1", values["UL_REDATA_API_URL"])

    def test_a_private_instance_does_not_take_the_production_key(self) -> None:
        """It names a row in a *database*, and a private REData starts with an empty one -
        so the inherited key authenticates against nothing and every call is a 401.
        `_provision_redata_key` mints the real one into the file afterwards."""
        values = devenv.redata_settings(devenv.REDATA_OWN, 31001, ("https://redata.example.test", "rdk_production"))

        self.assertNotIn("UL_REDATA_API_KEY", values)

    def test_no_redata_means_no_url(self) -> None:
        """An environment created with `--no-redata` must not point anywhere.

        Written explicitly rather than omitted: everything else in the file is
        inherited from the host's .env, which carries a working production URL,
        so leaving the key out would silently give this mode a live REData.
        """
        values = devenv.redata_settings(devenv.REDATA_NONE, None, ("https://redata.example.test", "rdk_production"))

        self.assertEqual(values["UL_REDATA_API_URL"], "")
        self.assertEqual(devenv.redata_api_url(None), "")

    def test_no_redata_overrides_the_inherited_url_in_the_written_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.env"
            source.write_text("UL_REDATA_API_URL=https://redata.urbanlens.org\n", encoding="utf-8")
            destination = Path(directory) / "out.env"

            devenv._write_env_file(destination, devenv.redata_settings(devenv.REDATA_NONE, None, ("", "")), source)

            written = destination.read_text(encoding="utf-8")

        self.assertIn("UL_REDATA_API_URL=\n", written)
        self.assertNotIn("redata.urbanlens.org", written)


class RedataCredentialInheritanceTests(SimpleTestCase):
    """Where an environment's production REData credentials come from.

    The key is a real production credential. It has exactly one destination -
    the new environment's own `.env` - and must not reach the run record, the
    registry, the JSON the CLI prints, or the log, all of which are read and
    pasted around casually.
    """

    def _source(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / ".env"
        source.write_text(text, encoding="utf-8")
        return source

    def test_the_hosts_env_file_supplies_both(self) -> None:
        source = self._source("UL_REDATA_API_URL=https://redata.example.test\nUL_REDATA_API_KEY=rdk_from_file\n")

        # Blanked rather than deleted: an empty value falls through to the file
        # exactly as an unset one does, and this host really does export these.
        with mock.patch.dict("os.environ", {"UL_REDATA_API_URL": "", "UL_REDATA_API_KEY": ""}):
            self.assertEqual(devenv.production_redata_credentials(source), ("https://redata.example.test", "rdk_from_file"))

    def test_the_process_environment_wins(self) -> None:
        """Exporting one names which REData this environment should use, which is
        more specific than whatever the host checkout happens to be set to."""
        source = self._source("UL_REDATA_API_URL=https://from-file.test\nUL_REDATA_API_KEY=rdk_from_file\n")

        with mock.patch.dict("os.environ", {"UL_REDATA_API_URL": "https://from-env.test", "UL_REDATA_API_KEY": "rdk_from_env"}):
            self.assertEqual(devenv.production_redata_credentials(source), ("https://from-env.test", "rdk_from_env"))

    def test_a_trailing_slash_is_dropped(self) -> None:
        """The gateway joins paths onto this; two slashes are a 404 from a service that is up."""
        with mock.patch.dict("os.environ", {"UL_REDATA_API_URL": "https://redata.example.test/", "UL_REDATA_API_KEY": "k"}):
            self.assertEqual(devenv.production_redata_credentials(Path("/nonexistent/.env"))[0], "https://redata.example.test")

    def test_nothing_anywhere_is_empty_rather_than_an_error(self) -> None:
        """A host without REData configured still gets an environment."""
        with mock.patch.dict("os.environ", {"UL_REDATA_API_URL": "", "UL_REDATA_API_KEY": ""}):
            self.assertEqual(devenv.production_redata_credentials(Path("/nonexistent/.env")), ("", ""))

    def test_missing_credentials_warn_and_do_not_fail_the_create(self) -> None:
        """Forty minutes of stack is not worth discarding over one inert integration."""
        status, detail = devenv.redata_mode_note(devenv.REDATA_PRODUCTION, ("", ""), None)

        self.assertEqual(status, "warn")
        self.assertIn("inert", detail)
        self.assertIn("--own-redata", detail, "the note has to say what to do instead")

    def test_a_half_configured_host_still_warns(self) -> None:
        """A URL with no key is a 401 from a service that is up - the same inertness, harder to read."""
        status, detail = devenv.redata_mode_note(devenv.REDATA_PRODUCTION, ("https://redata.example.test", ""), None)

        self.assertEqual(status, "warn")
        self.assertIn("UL_REDATA_API_KEY", detail)

    def test_the_key_never_reaches_the_run_record(self) -> None:
        """This detail string goes into the JSON the CLI prints and into the log file."""
        status, detail = devenv.redata_mode_note(devenv.REDATA_PRODUCTION, ("https://redata.example.test", "rdk_do_not_print"), None)

        self.assertEqual(status, "ok")
        self.assertNotIn("rdk_do_not_print", detail)
        self.assertIn("https://redata.example.test", detail, "the URL is not a secret, and is the thing worth knowing")

    def test_the_key_never_reaches_the_registry(self) -> None:
        """`list` prints these entries verbatim, and the registry file is world-readable."""
        env = devenv.DevEnv(
            slug="a1b2c3",
            path="unused",
            hostname="h",
            url="https://h",
            app_port=31000,
            created_at="now",
            branch="main",
            redata_mode=devenv.REDATA_PRODUCTION,
        )

        serialised = json.dumps(env.as_dict())

        self.assertNotIn("UL_REDATA_API_KEY", serialised)
        self.assertNotIn("redata_api_key", serialised)
        self.assertEqual(env.redata_port, None, "an environment with no REData of its own has no REData port")

    def test_the_key_does_reach_the_environments_own_env_file(self) -> None:
        """The one place it belongs - otherwise the environment is pointed at a service it cannot authenticate to."""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".env"

            devenv._write_env_file(destination, devenv.redata_settings(devenv.REDATA_PRODUCTION, None, ("https://redata.example.test", "rdk_secret")), None)

            self.assertIn("UL_REDATA_API_KEY=rdk_secret", destination.read_text(encoding="utf-8"))


class RedataModeReportingTests(SimpleTestCase):
    """`list` has to say which environments own a REData stack.

    Otherwise the answer to "is this environment costing eight idle containers"
    is a docker archaeology exercise, which is the same shape of problem the
    registry exists to remove.
    """

    def _listing(self, entries: dict) -> list[dict]:
        with (
            mock.patch.object(devenv, "_load_registry", return_value=entries),
            mock.patch.object(devenv.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")),
        ):
            return devenv.list_envs()

    def test_the_mode_is_reported(self) -> None:
        listed = self._listing({"a": {"slug": "a", "redata_mode": devenv.REDATA_OWN}})

        self.assertEqual(listed[0]["redata_mode"], devenv.REDATA_OWN)

    def test_an_entry_from_before_modes_existed_reads_unknown(self) -> None:
        """Its `redata_port` cannot answer this - one was allocated either way."""
        listed = self._listing({"a": {"slug": "a", "redata_port": 31001}})

        self.assertEqual(listed[0]["redata_mode"], "unknown")


class DevEnvCliFlagTests(SimpleTestCase):
    """The flags decide whether an environment spends somebody else's API quota.

    Worth pinning rather than discovering: a create takes about forty minutes,
    so "which mode did that use" is not a question to answer by running it.
    """

    def _mode(self, argv: list[str]) -> str:
        import dev_env

        return dev_env.redata_mode(dev_env.build_parser().parse_args(argv))

    def test_the_default_is_production(self) -> None:
        self.assertEqual(self._mode(["create"]), devenv.REDATA_PRODUCTION)

    def test_own_redata_asks_for_a_private_instance(self) -> None:
        self.assertEqual(self._mode(["create", "--own-redata"]), devenv.REDATA_OWN)

    def test_no_redata_keeps_its_meaning(self) -> None:
        self.assertEqual(self._mode(["create", "--no-redata"]), devenv.REDATA_NONE)

    def test_the_two_flags_cannot_be_combined(self) -> None:
        """A private instance and no REData at all is not a state anything can honour."""
        with self.assertRaises(SystemExit):
            self._mode(["create", "--own-redata", "--no-redata"])


class ContainerNamingTests(SimpleTestCase):
    """Three sites needed this name and had already drifted apart.

    The isolation override writes `ul_<slug>_<service>`, the log capture asks
    for `ul_<slug>_app`, and `list_envs` looked for `agent_<slug>` - a prefix
    nothing creates, so every environment was reported as not running whatever
    its actual state.
    """

    def test_the_name_matches_what_the_override_pins(self) -> None:
        """Read against the real compose file, since the override only emits
        services that file actually defines."""
        override = devenv._isolation_override("demo", _BIN.parent)

        self.assertIn(f"container_name: {devenv.container_name('demo', 'app')}", override)
        self.assertIn(f"container_name: {devenv.container_name('demo', 'db')}", override)

    def test_a_hyphenated_service_becomes_an_underscore(self) -> None:
        """Compose service names carry hyphens; container names here do not."""
        self.assertEqual(devenv.container_name("demo", "app-ws"), "ul_demo_app_ws")

    def test_list_matches_a_running_environment(self) -> None:
        """`agent_<slug>` matched nothing, so `running` was always False."""
        listing = "ul_demo_app\nul_demo_db\nsomething_else\n"

        self.assertIn(devenv.container_name("demo", "app"), listing)
        self.assertNotIn("agent_demo", listing)


class StagingDbContainerTests(SimpleTestCase):
    """The data-preservation check counts rows in a container it has to name correctly.

    Naming it wrong is silent: no container matches, every count comes back -1,
    and the comparison used to read that as nothing to complain about. Both
    sides used `UL_ENVIRONMENT` alone, so any deployment setting
    `UL_CONTAINER_NAME` - the variable that exists precisely to override it -
    passed the check vacuously.
    """

    def _env(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write(text)
        handle.close()
        return Path(handle.name)

    def test_container_name_wins_over_environment(self) -> None:
        env = self._env("UL_ENVIRONMENT=staging\nUL_CONTAINER_NAME=staging_blue\n")

        self.assertEqual(_db_container(env), "urbanlens_staging_blue_db")

    def test_environment_is_the_fallback(self) -> None:
        self.assertEqual(_db_container(self._env("UL_ENVIRONMENT=staging\n")), "urbanlens_staging_db")

    def test_production_is_the_last_resort(self) -> None:
        """Matching docker-compose.yml's own default chain."""
        self.assertEqual(_db_container(self._env("")), "urbanlens_production_db")


class StagingBaseUrlTests(SimpleTestCase):
    """The URL the pipeline probes itself has to be one ALLOWED_HOSTS accepts.

    A bare IP fails ALLOWED_HOSTS with a 400 on every request, which every
    downstream check (health wait, smoke pages) then reads as "down" - even
    though the deployment is up and serving real traffic at its real hostname.
    """

    def test_site_url_wins_when_set(self) -> None:
        self.assertEqual(_staging_base_url("https://staging.urbanlens.org", "21830"), "https://staging.urbanlens.org")

    def test_falls_back_to_localhost_not_the_bare_ip(self) -> None:
        self.assertEqual(_staging_base_url("", "21830"), "http://localhost:21830")

    def test_no_port_and_no_site_url_means_nothing_to_probe(self) -> None:
        self.assertEqual(_staging_base_url("", ""), "")


class VerifiedTablesTests(SimpleTestCase):
    """A table name that matches nothing counts -1, which the check skipped."""

    def test_every_verified_table_exists_in_the_schema(self) -> None:
        from django.apps import apps

        known = {model._meta.db_table for model in apps.get_models()}

        missing = [table for table in _VERIFIED_TABLES if table not in known]

        self.assertEqual(missing, [], "a name no model owns can never be counted, so it silently drops out of the comparison")

    def test_the_pins_table_is_named_the_way_the_model_names_it(self) -> None:
        """`dashboard_pins` was checked for a year and exists nowhere."""
        from urbanlens.dashboard.models.pin.model import Pin

        self.assertIn(Pin._meta.db_table, _VERIFIED_TABLES)


class DestroyFailureTests(SimpleTestCase):
    """A teardown that failed must not be recorded as an environment removed.

    `destroy` set `containers: True` whatever `docker compose down` returned and
    popped the registry entry regardless - so a failed teardown left containers
    running with nothing left recording that they exist, and the CLI exited 0.
    The registry entry is the only handle anyone has on them.
    """

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        env_dir = root / "doomed" / "UrbanLens"
        env_dir.mkdir(parents=True)
        (root / "doomed" / "UrbanLens" / ".env").write_text("UL_ENVIRONMENT=development\n", encoding="utf-8")
        self.registry = root / "registry.json"
        self.registry.write_text(json.dumps({"doomed": {"slug": "doomed", "path": str(root / "doomed")}}), encoding="utf-8")
        self._registry_patch = mock.patch.object(devenv, "REGISTRY", self.registry)
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.root = root

    def _destroy(self, returncode: int):
        completed = subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="network in use")
        with (
            mock.patch.object(devenv.subprocess, "run", return_value=completed),
            # Patched on `router`, not `devenv`: destroy imports it inside the
            # function, so there is no module-level attribute to replace.
            mock.patch.object(router, "write_routes"),
        ):
            return devenv.destroy("doomed", root=self.root)

    def test_a_failed_teardown_keeps_the_registry_entry(self) -> None:
        result = self._destroy(returncode=1)

        self.assertFalse(result["containers"])
        self.assertFalse(result["registry"])
        self.assertIn("doomed", json.loads(self.registry.read_text()), "the entry is the only record those containers exist")

    def test_a_failed_teardown_reports_why(self) -> None:
        result = self._destroy(returncode=1)

        self.assertTrue(result.get("errors"), "the compose output is what tells someone what to do next")
        self.assertIn("network in use", " ".join(result["errors"]))

    def test_a_failed_teardown_marks_the_entry_orphaned(self) -> None:
        self._destroy(returncode=1)

        self.assertEqual(json.loads(self.registry.read_text())["doomed"]["status"], "orphaned")

    def test_a_failed_teardown_leaves_the_files_alone(self) -> None:
        """Deleting the checkout of a stack still running removes the way to stop it."""
        self._destroy(returncode=1)

        self.assertTrue((self.root / "doomed" / "UrbanLens").is_dir())

    def test_a_clean_teardown_still_removes_everything(self) -> None:
        result = self._destroy(returncode=0)

        self.assertTrue(result["containers"])
        self.assertTrue(result["registry"])
        self.assertNotIn("doomed", json.loads(self.registry.read_text()))


class DemoAccountSeedingStepTests(SimpleTestCase):
    """A created environment must hand back a way in, and must survive not having one.

    The account is the difference between a URL onto an empty database and an
    environment somebody can look at. It is also the step most likely to fail
    for a reason that has nothing to do with the stack (an unreachable
    catalog), which is why nothing here is allowed to fail the create.
    """

    def _run_with_output(self, output: str, status: str = "ok") -> tuple[mock.Mock, devenv.DevEnv, mock.Mock]:
        from opslib.steps import StepResult

        result = StepResult(name="seed-demo-account", status=status, seconds=1.0, output_tail=output)
        run = mock.Mock()
        run.run_step.return_value = result
        env = devenv.DevEnv(slug="a1b2c3", path="unused", hostname="h", url="https://h", app_port=31000, created_at="now", branch="main")
        devenv._seed_demo_account(run, env)
        return run, env, result

    def test_the_password_is_derived_from_the_slug(self) -> None:
        """Derivable on purpose: whoever reads the hostname can work out the login."""
        self.assertEqual(devenv.seed_password("a1b2c3"), "demo-a1b2c3")

    def test_a_seeded_account_is_recorded_on_the_environment(self) -> None:
        _, env, result = self._run_with_output('UL_DEV_SEED={"username": "demo", "created": true, "pins": 12, "landmark": "Hudson River State Hospital", "catalog": "imported 11"}')

        self.assertEqual((env.login_username, env.login_password), ("demo", "demo-a1b2c3"))
        self.assertIn("demo / demo-a1b2c3", result.detail)
        self.assertIn("12 pin(s)", result.detail)

    def test_the_credentials_are_passed_as_environment_variables(self) -> None:
        """Not interpolated into the script, where they would have to be quoted correctly."""
        run, _, _ = self._run_with_output('UL_DEV_SEED={"created": true, "username": "demo"}')

        command = run.run_step.call_args.args[1]
        self.assertIn("UL_DEV_SEED_PASSWORD=demo-a1b2c3", command)
        self.assertIn(devenv.container_name("a1b2c3", "app"), command)

    def test_a_failed_seed_claims_no_login(self) -> None:
        """Reporting credentials that do not work is worse than reporting none."""
        _, env, result = self._run_with_output("Traceback (most recent call last): ...", status="warn")

        self.assertEqual((env.login_username, env.login_password), ("", ""))
        self.assertEqual(result.status, "warn")
        self.assertIn("created by hand", result.detail)

    def test_an_already_seeded_environment_is_not_reported_as_freshly_created(self) -> None:
        _, env, _ = self._run_with_output('UL_DEV_SEED={"created": false, "username": "demo"}')

        self.assertEqual(env.login_username, "")

    def test_unparseable_output_is_not_read_as_success(self) -> None:
        self.assertEqual(devenv._parse_seed_summary("UL_DEV_SEED={not json"), {})
        self.assertEqual(devenv._parse_seed_summary(""), {})

    def test_the_environment_records_its_own_manifest_path(self) -> None:
        """So a later import in this environment tops the same manifest up rather than starting a second one."""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".env"
            devenv._write_env_file(destination, {"UL_DEMO_LOCATIONS_FILE": "/app/demo-locations.json"}, None)

            self.assertIn("UL_DEMO_LOCATIONS_FILE=/app/demo-locations.json", destination.read_text(encoding="utf-8"))
