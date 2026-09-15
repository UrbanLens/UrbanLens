"""The settings that bound the connection pool, and the ones that let us see it."""

from __future__ import annotations

import json
import pathlib
import re

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.database_roles import REQUEST_DEADLINE_SECONDS, declared_roles

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

#: `max_connections` (100) less `superuser_reserved_connections` (3).
USABLE_CONNECTIONS = 97

#: The services given the owner's credentials: the one-shot setup job, and the test stack against its own database.
OWNER_SERVICES = frozenset({"db-setup", "test-runner"})


def _start_command() -> str:
    """The gunicorn command line the app container runs."""
    package = json.loads((REPO_ROOT / "package.json").read_text())
    return package["scripts"]["start"]


def _compose() -> dict:
    """The compose file, parsed."""
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _environment(service: dict) -> dict[str, str]:
    """A service's environment as a mapping, in whichever form compose was given it."""
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return dict(entry.split("=", 1) for entry in environment)
    return {key: str(value) for key, value in environment.items()}


def _command(service: dict) -> str:
    command = service.get("command") or ""
    return command if isinstance(command, str) else " ".join(command)


def _tier_services() -> dict[str, dict]:
    """Every service that reaches the application database as one of the per-tier roles."""
    return {
        name: service
        for name, service in _compose()["services"].items()
        if "UL_DB_HOST" in _environment(service) and name not in OWNER_SERVICES
    }


def _limits() -> dict[str, int]:
    return {role.process_role: role.connection_limit for role in declared_roles()}


class TheRequestThreadPopulationIsBoundedTests(SimpleTestCase):
    """Each request thread keeps one connection, so the thread count is the web tier's connection count."""

    def test_the_worker_runs_threads(self) -> None:
        """A gevent greenlet's connection is local to a greenlet that ends with its request, so it is never reused."""
        self.assertRegex(_start_command(), r"(-k|--worker-class)\s+gthread\b")

    def test_the_web_tier_fits_inside_its_role_with_room_to_spare(self) -> None:
        """Arithmetic rather than a hardcoded expectation, so raising WEB_CONCURRENCY or the thread count fails here.

        Strictly under: timeout_utils' executor threads connect separately from the request they serve."""
        command = _start_command()
        match = re.search(r"--threads\s+(\d+)", command)
        self.assertIsNotNone(
            match, f"could not read --threads out of {command!r}; gthread's default of 1 is not a choice"
        )
        assert match is not None
        per_worker = int(match.group(1))
        workers = int(str(_compose()["x-app-env"]["WEB_CONCURRENCY"]).split(":-")[-1].rstrip("}"))

        self.assertLess(
            per_worker * workers,
            _limits()["web"],
            f"{workers} workers x {per_worker} threads reaches ul_web's connection limit, so ordinary load would fail requests",
        )

    def test_overflow_queues_rather_than_connecting(self) -> None:
        self.assertIn("--backlog", _start_command())


class EveryTierHasItsOwnConnectionBudgetTests(SimpleTestCase):
    """A tier that exhausts its role's limit fails its own connections, and nobody else's."""

    def test_every_service_that_reaches_the_database_logs_in_as_its_own_tier(self) -> None:
        limits = _limits()
        for name, service in _tier_services().items():
            environment = _environment(service)
            role = environment.get("UL_PROCESS_ROLE")
            with self.subTest(service=name):
                self.assertEqual(
                    environment.get("UL_DB_USER"),
                    f"ul_{role}",
                    "a tier logging in as another tier's role spends that tier's budget",
                )
                self.assertIn(
                    role, limits, "no DatabaseRole declares this tier, so db-setup never creates its login role"
                )

    def test_no_tier_is_given_the_owners_password(self) -> None:
        for name, service in _tier_services().items():
            with self.subTest(service=name):
                self.assertTrue(
                    _environment(service)["UL_DB_PASS"].startswith("${UL_DB_APP_PASS"),
                    "the owner is a superuser; a tier holding its password can bypass every limit here",
                )

    def test_only_db_setup_and_the_test_stack_hold_the_owner(self) -> None:
        services = _compose()["services"]
        for name in OWNER_SERVICES:
            environment = _environment(services[name])
            with self.subTest(service=name):
                self.assertTrue(environment["UL_DB_USER"].startswith("${UL_DB_USER"))
                self.assertTrue(environment["UL_DB_PASS"].startswith("${UL_DB_PASS"))

    def test_every_tier_waits_for_its_role_to_exist(self) -> None:
        for name, service in _tier_services().items():
            with self.subTest(service=name):
                condition = ((service.get("depends_on") or {}).get("db-setup") or {}).get("condition")
                self.assertEqual(
                    condition,
                    "service_completed_successfully",
                    "started before db-setup, it may log in as a role that does not exist yet",
                )

    def test_db_setup_does_the_owners_work_once_and_the_app_none_of_it(self) -> None:
        services = _compose()["services"]
        self.assertIn("--db-only", _command(services["db-setup"]))
        self.assertEqual(services["db-setup"].get("restart"), "no")
        self.assertIn(
            "--no-db", _command(services["app"]), "ul_web cannot migrate, so an app that tried would never start"
        )

    def test_each_tiers_concurrency_fits_inside_its_limit(self) -> None:
        """A Celery container needs its pool plus its parent; daphne, beat and the exporter need one each."""
        demand: dict[str, int] = {}
        for name, service in _tier_services().items():
            if name == "app":
                continue
            role = _environment(service)["UL_PROCESS_ROLE"]
            concurrency = re.search(r"--concurrency[= ](\d+)", _command(service))
            demand[role] = demand.get(role, 0) + (int(concurrency.group(1)) + 1 if concurrency else 1)

        limits = _limits()
        for role, needed in demand.items():
            with self.subTest(role=role):
                self.assertLessEqual(
                    needed,
                    limits[role],
                    f"{role}'s containers can open {needed} connections against a limit of {limits[role]}",
                )

    def test_the_limits_fit_inside_a_stock_postgres(self) -> None:
        """db-setup checks the live server too; this fails before a deploy rather than during one."""
        total = sum(_limits().values())
        self.assertLessEqual(
            total,
            USABLE_CONNECTIONS,
            f"role limits sum to {total}, over the {USABLE_CONNECTIONS} a stock Postgres accepts from non-superusers",
        )

    def test_the_request_deadline_is_nginxs(self) -> None:
        """A statement still running after nginx gave up on its request has nobody to answer."""
        conf = (REPO_ROOT / "src/urbanlens/config/nginx/django.conf").read_text()
        match = re.search(r"location / \{\s*proxy_read_timeout (\d+)s;", conf)
        self.assertIsNotNone(match, "could not read the app location's proxy_read_timeout")
        assert match is not None
        self.assertEqual(int(match.group(1)), REQUEST_DEADLINE_SECONDS)


class TheSocketTierKeepsItsDatabaseConnectionTests(SimpleTestCase):
    """Channels closes an expired connection around every database hop, and daphne makes every hop on one thread.

    At ``CONN_MAX_AGE=0`` that is a Postgres login per hop, several per handshake, queued behind each other."""

    def test_app_ws_reuses_its_connection(self) -> None:
        environment = _environment(_compose()["services"]["app-ws"])

        self.assertGreater(int(environment.get("UL_DB_CONN_MAX_AGE", "0")), 0)
        self.assertEqual(
            environment.get("UL_DB_CONN_HEALTH_CHECKS"),
            "true",
            "a connection Postgres dropped would fail the next socket's lookup rather than be replaced",
        )


class TheWebTierKeepsItsDatabaseConnectionTests(SimpleTestCase):
    """At ``CONN_MAX_AGE=0`` every request pays a Postgres login, which costs more CPU than a poll's own Python."""

    def test_app_reuses_its_connection(self) -> None:
        environment = _environment(_compose()["services"]["app"])

        self.assertGreater(int(environment.get("UL_DB_CONN_MAX_AGE", "0")), 0)
        self.assertEqual(
            environment.get("UL_DB_CONN_HEALTH_CHECKS"),
            "true",
            "a connection Postgres dropped would fail the next request rather than be replaced",
        )


class TheMetricsExporterIsGatedTests(SimpleTestCase):
    """A precondition in a comment gates nothing."""

    def test_celery_metrics_is_behind_a_compose_profile(self) -> None:
        service = _compose()["services"]["celery-metrics"]
        self.assertEqual(
            service.get("profiles"),
            ["metrics"],
            "celery-metrics exits when UL_METRICS_ENABLED is off and restarts unless-stopped, so "
            "without a profile `docker compose up` starts it into a permanent crash loop - which is "
            "what happened on staging for 2,578 restarts (N15)",
        )

    def test_it_still_restarts_when_it_is_deployed(self) -> None:
        """The profile is the gate; the restart policy is still wanted inside it."""
        service = _compose()["services"]["celery-metrics"]
        self.assertEqual(service.get("restart"), "unless-stopped")


class TheProxyLogCanExplainATimeoutTests(SimpleTestCase):
    """A 504 nginx generated and one the app returned must be distinguishable."""

    def test_the_access_log_carries_upstream_timing(self) -> None:
        nginx_conf = (REPO_ROOT / "src/urbanlens/config/nginx/nginx.conf").read_text()

        for variable in ("$request_time", "$upstream_response_time", "$upstream_status"):
            self.assertIn(
                variable,
                nginx_conf,
                f"log_format is missing {variable}; without it a proxy timeout and an application "
                "error are the same line in the log",
            )


class TheDatabaseSaysWhoIsConnectedTests(SimpleTestCase):
    """`pg_stat_activity` should name the process, not only the login role."""

    def test_connections_are_labelled_with_the_process_role(self) -> None:
        base = (REPO_ROOT / "src/urbanlens/UrbanLens/settings/base.py").read_text()

        self.assertIn(
            "application_name",
            base,
            "db-setup and the test stack both log in as the owner, so without application_name "
            "pg_stat_activity cannot say which of them holds a connection",
        )
        self.assertIn("UL_PROCESS_ROLE", base)

    def test_the_setting_reaches_django(self) -> None:
        """Asserted against live settings, not the source, so a typo in the key fails.

        `OPTIONS` is passed to the driver verbatim; a misspelled key is accepted by Django and rejected by
        psycopg at connect time, which is a runtime failure rather than a config one."""
        from django.conf import settings

        options = settings.DATABASES["default"]["OPTIONS"]
        if not isinstance(options, dict):
            self.fail(f"DATABASES['default']['OPTIONS'] is a {type(options).__name__}, not a dict")
        self.assertIn("application_name", options)
        self.assertTrue(
            str(options["application_name"]).startswith("urbanlens-"),
            f"expected an urbanlens- prefixed name, got {options['application_name']!r}",
        )


class TheRequestPathOutweighsBackgroundWorkTests(SimpleTestCase):
    """Ceilings alone let a Celery worker and the request path compete as equals.

    A weight decides who yields when that runs short."""

    #: Services whose weight must exceed every background worker's.
    FOREGROUND = ("app", "app-ws", "db")

    #: Background work: progress-tracked, and nobody is watching a spinner for it.
    BACKGROUND = ("media-worker", "media-worker-batch", "celery-metrics", "ai-worker")

    def _weights(self) -> dict[str, int]:
        """Each service's default cpu_shares, read out of its `${VAR:-N}` form."""
        services = _compose()["services"]
        weights = {}
        for name, service in services.items():
            raw = service.get("cpu_shares")
            if raw is None:
                continue
            weights[name] = int(str(raw).split(":-")[-1].rstrip("}"))
        return weights

    def test_the_foreground_services_carry_a_weight(self) -> None:
        weights = self._weights()
        for name in self.FOREGROUND:
            self.assertIn(name, weights, f"{name} has no cpu_shares, so it yields to background work equally")

    def test_every_foreground_service_outweighs_every_background_one(self) -> None:
        weights = self._weights()
        for foreground in self.FOREGROUND:
            for background in self.BACKGROUND:
                self.assertGreater(
                    weights[foreground],
                    weights.get(background, 1024),
                    f"{background} is weighted at or above {foreground}; under contention the "
                    "request path would yield to background work",
                )

    def test_the_test_services_are_left_unweighted(self) -> None:
        """They are not production contenders, and weighting them only slows the suite."""
        weights = self._weights()
        for name in _compose()["services"]:
            if name.startswith("test-"):
                self.assertNotIn(name, weights)

    def test_the_database_and_app_have_a_memory_floor(self) -> None:
        """`mem_limit` is a ceiling; under host pressure a floor is what protects them."""
        services = _compose()["services"]
        for name in ("app", "db"):
            self.assertIn(
                "mem_reservation",
                services[name],
                f"{name} has a memory ceiling but no floor, so the kernel may reclaim from it first",
            )
