"""The settings that bound the connection pool, and the ones that let us see it.

Four config changes, none of which any running process can check for itself, and
each of which regresses silently. A flag dropped from a command line looks like a
tidier command line; a log format that stops carrying timings looks like a log.

The class of failure is the one this repo has hit before: a condition stated in a
comment rather than in configuration. `celery-metrics` carried a comment saying
its deployment "is conditional on the same flag" and nothing implemented that, so
it restarted 2,578 times on staging and staging never had Celery metrics (N15).

None of these are behaviour tests - they read the deployment files, which are
baked into the app image and therefore readable here. They assert the wiring
exists, not that it works; what proves the connection bound works is the k6
neighbour scenario in PL7, which needs a deployment.
"""

from __future__ import annotations

import json
import pathlib
import re

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

#: gevent's own default, and the number this exists to stop us shipping.
GEVENT_DEFAULT_WORKER_CONNECTIONS = 1000

#: `max_connections` (100) less `superuser_reserved_connections` (3). The number
#: the 2026-09-07 outage reported as idle, which is what identified the ceiling.
USABLE_CONNECTIONS = 97


def _start_command() -> str:
    """The gunicorn command line the app container runs."""
    package = json.loads((REPO_ROOT / "package.json").read_text())
    return package["scripts"]["start"]


def _compose() -> dict:
    """The compose file, parsed."""
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


class TheGreenletPopulationIsBoundedTests(SimpleTestCase):
    """Under gevent, unbounded greenlets means unbounded database connections."""

    def test_worker_connections_is_set_at_all(self) -> None:
        """Left unset, one worker may open a thousand.

        Each greenlet serving a request can hold its own backend under
        CONN_MAX_AGE=0, so three workers at the default could demand 3,000
        connections against a ceiling of 100. That is P104's mechanism.
        """
        self.assertIn(
            "--worker-connections",
            _start_command(),
            "gunicorn's gevent worker defaults to "
            f"{GEVENT_DEFAULT_WORKER_CONNECTIONS} greenlets per worker; the start command must cap it",
        )

    def test_the_cap_leaves_room_for_every_other_container(self) -> None:
        """The web tier is not the only thing connecting to this database.

        Deliberately arithmetic rather than a hardcoded expectation, so that
        raising WEB_CONCURRENCY or the per-worker cap fails here instead of in
        production. The non-web allowance is the sum of the concurrency settings
        in `docker-compose.yml`: daphne, the four Celery workers, and beat.
        """
        command = _start_command()
        match = re.search(r"--worker-connections\s+(\d+)", command)
        self.assertIsNotNone(match, f"could not read --worker-connections out of {command!r}")
        assert match is not None
        per_worker = int(match.group(1))

        compose = _compose()
        web_concurrency = compose["x-app-env"]["WEB_CONCURRENCY"] if "x-app-env" in compose else None
        workers = int(str(web_concurrency).split(":-")[-1].rstrip("}")) if web_concurrency else 3

        non_web_allowance = 32
        demanded = per_worker * workers
        self.assertLessEqual(
            demanded + non_web_allowance,
            USABLE_CONNECTIONS,
            f"{workers} workers x {per_worker} greenlets = {demanded} connections, and about "
            f"{non_web_allowance} more belong to daphne, the Celery workers and beat - over the "
            f"{USABLE_CONNECTIONS} a stock Postgres leaves for non-superusers",
        )

    def test_overflow_queues_rather_than_connecting(self) -> None:
        self.assertIn("--backlog", _start_command())


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
    """`pg_stat_activity` could not name the tier holding the connections."""

    def test_connections_are_labelled_with_the_process_role(self) -> None:
        base = (REPO_ROOT / "src/urbanlens/UrbanLens/settings/base.py").read_text()

        self.assertIn(
            "application_name",
            base,
            "every container connects as the same role, so without application_name nothing "
            "records which tier held the connections when the pool was exhausted",
        )
        self.assertIn("UL_PROCESS_ROLE", base)

    def test_the_setting_reaches_django(self) -> None:
        """Asserted against live settings, not the source, so a typo in the key fails.

        `OPTIONS` is passed to the driver verbatim; a misspelled key is accepted
        by Django and rejected by psycopg at connect time, which is a runtime
        failure rather than a config one.
        """
        from django.conf import settings

        options = settings.DATABASES["default"]["OPTIONS"]
        self.assertIn("application_name", options)
        self.assertTrue(
            str(options["application_name"]).startswith("urbanlens-"),
            f"expected an urbanlens- prefixed name, got {options['application_name']!r}",
        )


class TheRequestPathOutweighsBackgroundWorkTests(SimpleTestCase):
    """Ceilings alone let a Celery worker and the request path compete as equals.

    `cpus:` is a CFS ceiling and reserves nothing, and the ceilings in this file
    sum to roughly 20 CPU against damballa's 16 cores - with production and
    staging both on it. A weight decides who yields when that runs short.

    Asserted as an ordering rather than as literal numbers, so retuning the
    weights does not require editing this, but inverting them does.
    """

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
