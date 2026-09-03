"""The Prometheus scrape endpoint: who may read it, and what it reports.

Several separable claims, each of which fails differently and silently:

1. The route does not exist unless ``UL_METRICS_ENABLED``
   (:class:`MetricsRouteRegistrationTests`) - the outermost gate, and the one
   that cannot be undone by a misconfigured guard.
2. The token and network gates actually refuse
   (:class:`MetricsTokenGateTests`, :class:`MetricsNetworkGateTests`). A
   ``/metrics`` body is a map of the application, so "the guard is there" is
   not the same claim as "the guard says no".
3. The network gate resolves the client address through the trusted-proxy hop
   count, so a forged ``X-Forwarded-For`` cannot spoof its way into the
   allowlist (:class:`MetricsNetworkGateTests`) - the failure mode that makes
   an IP allowlist worthless.
4. The exporter aggregates across processes (:class:`MetricsRegistryTests`).
   This is the one that would otherwise look fine forever: production runs
   ``WEB_CONCURRENCY`` gunicorn workers, and serving the per-process default
   registry answers every scrape with one worker's share of the traffic.
5. The deployment actually wires 4 up (:class:`MetricsDeploymentWiringTests`) -
   the gunicorn hooks, the entrypoint's directory handling, and nginx's refusal
   to serve the path publicly. Python-level tests cannot reach any of these,
   and each is load-bearing.
"""

from __future__ import annotations

import ipaddress
import os
import pathlib
import re
import sys
import tempfile
from unittest import mock

from django.conf import settings
from django.core.checks import Error
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings
from django.urls import NoReverseMatch, reverse
import prometheus_client
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.UrbanLens.settings import _metrics
from urbanlens.dashboard.checks import check_metrics_endpoint_is_guarded
from urbanlens.dashboard.controllers.metrics import MULTIPROC_DIR_ENV, MetricsController, _build_registry
from urbanlens.dashboard.services.security.client_ip import address_in_networks, client_ip, parse_networks

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


class MetricsSettingsExistTests(SimpleTestCase):
    """The settings the rest of this file overrides are real.

    ``override_settings`` invents any name it is given, so a suite that only
    ever overrides would pass identically against a settings module that never
    defined these - which is exactly how a guard reading
    ``settings.UL_METRICS_TOKEN`` ships reading ``""`` forever. Asserting they
    exist unoverridden is what makes the rest of these tests mean anything.
    """

    def test_settings_are_defined_without_an_override(self) -> None:
        for name in ("UL_METRICS_ENABLED", "UL_METRICS_TOKEN", "UL_METRICS_ALLOWED_CIDRS"):
            with self.subTest(setting=name):
                self.assertTrue(hasattr(settings, name), f"{name} is not defined in settings; every guard reading it is dead code.")

    def test_disabled_by_default(self) -> None:
        # The default has to be off: this endpoint describes the application,
        # and an install that never heard of it must not serve one.
        self.assertFalse(settings.UL_METRICS_ENABLED)


class MetricsRouteRegistrationTests(SimpleTestCase):
    """The URL exists only when the deployment opted in."""

    def test_route_is_absent_when_disabled(self) -> None:
        # The suite runs with metrics off, which is also the default posture.
        with self.assertRaises(NoReverseMatch):
            reverse("metrics")

    def test_urlconf_registers_the_route_under_the_flag(self) -> None:
        # The registration is import-time, so re-import the urlconf with the
        # flag flipped rather than asserting on the module already imported.
        import importlib

        from urbanlens.UrbanLens import urls as urlconf

        with mock.patch.object(urlconf.app_settings, "metrics_enabled", True):
            reloaded = importlib.reload(urlconf)
            try:
                names = {getattr(pattern, "name", None) for pattern in reloaded.urlpatterns}
                self.assertIn("metrics", names)
            finally:
                # Restore the module other tests (and this process) resolve against.
                importlib.reload(urlconf)


class MetricsDisabledPathTests(TestCase):
    """The disabled path really is unrouted, through the full request stack.

    A ``TestCase`` rather than a ``SimpleTestCase`` because the 404 this must
    fall through to renders the site's styled error page, which reads the
    database - which is itself the proof that nothing intercepted the path
    ahead of the catch-all.
    """

    def test_disabled_path_is_not_served(self) -> None:
        self.assertEqual(self.client.get("/metrics").status_code, 404)


@override_settings(UL_METRICS_TOKEN="s3cret-scrape-token", UL_METRICS_ALLOWED_CIDRS="")
class MetricsTokenGateTests(SimpleTestCase):
    """A configured bearer token is required, and actually compared."""

    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()

    def _get(self, **headers: str):
        return MetricsController.as_view()(self.factory.get("/metrics", **headers))

    def test_no_authorization_header_is_refused(self) -> None:
        response = self._get()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_wrong_token_is_refused(self) -> None:
        self.assertEqual(self._get(HTTP_AUTHORIZATION="Bearer wrong-token").status_code, 401)

    def test_token_prefix_is_refused(self) -> None:
        # A prefix of the real token must not pass; this is the shape a
        # non-constant-time comparison would eventually leak through.
        self.assertEqual(self._get(HTTP_AUTHORIZATION="Bearer s3cret-scrape").status_code, 401)

    def test_correct_token_in_the_wrong_scheme_is_refused(self) -> None:
        self.assertEqual(self._get(HTTP_AUTHORIZATION="Basic s3cret-scrape-token").status_code, 401)

    def test_bare_token_without_a_scheme_is_refused(self) -> None:
        self.assertEqual(self._get(HTTP_AUTHORIZATION="s3cret-scrape-token").status_code, 401)

    def test_correct_token_is_served(self) -> None:
        response = self._get(HTTP_AUTHORIZATION="Bearer s3cret-scrape-token")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response["Content-Type"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_scheme_match_is_case_insensitive(self) -> None:
        # RFC 7235 makes the scheme token case-insensitive; a scraper sending
        # "bearer" is conformant and must not be locked out.
        self.assertEqual(self._get(HTTP_AUTHORIZATION="bearer s3cret-scrape-token").status_code, 200)

    @override_settings(UL_METRICS_TOKEN="")
    def test_empty_token_setting_disables_the_gate(self) -> None:
        # Only acceptable because the CIDR gate or the startup check covers it;
        # asserted so that "empty means off" stays a deliberate behaviour rather
        # than something a refactor can flip either way unnoticed.
        self.assertEqual(self._get().status_code, 200)


@override_settings(UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="10.2.0.0/24", TRUSTED_PROXY_COUNT=1)
class MetricsNetworkGateTests(SimpleTestCase):
    """The allowlist admits the monitoring network and nothing else."""

    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()

    def _get(self, remote_addr: str, forwarded: str | None = None):
        headers = {"REMOTE_ADDR": remote_addr}
        if forwarded is not None:
            headers["HTTP_X_FORWARDED_FOR"] = forwarded
        return MetricsController.as_view()(self.factory.get("/metrics", **headers))

    def test_address_inside_the_allowlist_is_served(self) -> None:
        # One proxy hop, so the rightmost XFF entry is the one nginx appended.
        self.assertEqual(self._get("172.18.0.5", forwarded="10.2.0.9").status_code, 200)

    def test_address_outside_the_allowlist_is_refused_as_404(self) -> None:
        response = self._get("172.18.0.5", forwarded="203.0.113.7")
        self.assertEqual(response.status_code, 404, "A disallowed network must not learn that this URL exists.")

    def test_forged_forwarded_prefix_cannot_reach_the_allowlist(self) -> None:
        # The attacker controls everything left of what our own proxy appended.
        # Reading the leftmost entry - the naive implementation - would admit
        # this request; reading one hop from the right does not.
        response = self._get("172.18.0.5", forwarded="10.2.0.9, 203.0.113.7")
        self.assertEqual(response.status_code, 404)

    def test_missing_forwarded_chain_falls_back_to_the_socket_address(self) -> None:
        # A request that did not come through the proxy is judged on where it
        # actually came from, not on a header it may have invented.
        self.assertEqual(self._get("203.0.113.7").status_code, 404)
        self.assertEqual(self._get("10.2.0.9").status_code, 200)

    @override_settings(UL_METRICS_ALLOWED_CIDRS="10.2.0.0/24, , 127.0.0.1/32")
    def test_blank_and_multiple_entries_are_handled(self) -> None:
        self.assertEqual(self._get("172.18.0.5", forwarded="127.0.0.1").status_code, 200)

    @override_settings(UL_METRICS_ALLOWED_CIDRS="not-a-cidr")
    def test_an_unparseable_allowlist_refuses_everyone(self) -> None:
        # A typo must narrow what is reachable, never widen it.
        self.assertEqual(self._get("10.2.0.9", forwarded="10.2.0.9").status_code, 404)

    @override_settings(UL_METRICS_TOKEN="tok", UL_METRICS_ALLOWED_CIDRS="10.2.0.0/24")
    def test_both_gates_must_pass_when_both_are_configured(self) -> None:
        self.assertEqual(self._get("10.2.0.9", forwarded="10.2.0.9").status_code, 401)
        response = MetricsController.as_view()(
            self.factory.get("/metrics", REMOTE_ADDR="10.2.0.9", HTTP_X_FORWARDED_FOR="10.2.0.9", HTTP_AUTHORIZATION="Bearer tok"),
        )
        self.assertEqual(response.status_code, 200)


class ClientIpHelperTests(SimpleTestCase):
    """The shared address helpers, at the edges the gate depends on."""

    def test_parse_networks_drops_unparseable_entries(self) -> None:
        self.assertEqual(parse_networks("10.0.0.0/8, garbage, 192.168.0.0/16"), (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("192.168.0.0/16")))

    def test_parse_networks_returns_an_immutable_result(self) -> None:
        # It is cached and shared between callers; a list would let one caller
        # mutate another's allowlist.
        self.assertIsInstance(parse_networks("10.0.0.0/8"), tuple)

    def test_unparseable_address_is_in_no_network(self) -> None:
        # client_ip returns "unknown" when there is no socket address at all.
        self.assertFalse(address_in_networks("unknown", parse_networks("0.0.0.0/0")))

    def test_ipv6_allowlist_matches(self) -> None:
        self.assertTrue(address_in_networks("2001:db8::1", parse_networks("2001:db8::/32")))

    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_zero_hops_ignores_the_forwarded_header(self) -> None:
        request = RequestFactory().get("/", REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="10.2.0.9")
        self.assertEqual(client_ip(request), "172.18.0.5")


class MetricsRegistryTests(SimpleTestCase):
    """Scrapes cover every worker process, not whichever one answered."""

    def test_multiproc_dir_produces_an_aggregating_registry(self) -> None:
        # The claim that matters: with the env var set, the exporter builds a
        # registry backed by MultiProcessCollector rather than serving this
        # process's own metrics. Serving the default registry under gunicorn
        # would answer every scrape with roughly 1/WEB_CONCURRENCY of the truth.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.dict(os.environ, {MULTIPROC_DIR_ENV: directory.name}):
            registry = _build_registry()
        self.assertIsNot(registry, prometheus_client.REGISTRY)
        collectors = [type(collector).__name__ for collector in registry._collector_to_names]
        self.assertIn("MultiProcessCollector", collectors)

    def test_multiprocess_samples_written_by_another_process_are_collected(self) -> None:
        # The end-to-end version of the claim above, and the only form of it
        # that would have caught a broken aggregation path: write a sample the
        # way a *different* worker pid would, then confirm this process's
        # exporter reports it. A per-process registry cannot see this file.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.dict(os.environ, {MULTIPROC_DIR_ENV: directory.name}):
            # values.ValueClass is resolved at import time from the env var, so
            # build the multiprocess value explicitly rather than relying on the
            # module having been imported under these conditions.
            from prometheus_client.values import MultiProcessValue

            value_class = MultiProcessValue(process_identifier=lambda: 424242)
            sample = value_class("counter", "urbanlens_test_total", "urbanlens_test_total", (), (), "help text")
            sample.inc(7)
            payload = prometheus_client.generate_latest(_build_registry()).decode()
        self.assertIn("urbanlens_test_total", payload)
        self.assertIn("7.0", payload)

    def test_without_multiproc_dir_the_default_registry_is_used(self) -> None:
        # Correct only because it means genuinely one process - runserver, a
        # test. Asserted so the fallback stays deliberate.
        environ = {key: value for key, value in os.environ.items() if key != MULTIPROC_DIR_ENV}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertIs(_build_registry(), prometheus_client.REGISTRY)


class MetricsStartupCheckTests(SimpleTestCase):
    """An unguarded endpoint is a startup error where it counts."""

    @override_settings(UL_METRICS_ENABLED=True, UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="", IS_PRODUCTION=True)
    def test_enabled_and_unguarded_in_production_is_an_error(self) -> None:
        errors = check_metrics_endpoint_is_guarded()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Error)
        self.assertEqual(errors[0].id, "dashboard.E006")

    @override_settings(UL_METRICS_ENABLED=True, UL_METRICS_TOKEN="tok", UL_METRICS_ALLOWED_CIDRS="", IS_PRODUCTION=True)
    def test_a_token_alone_satisfies_the_check(self) -> None:
        self.assertEqual(check_metrics_endpoint_is_guarded(), [])

    @override_settings(UL_METRICS_ENABLED=True, UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="10.2.0.0/24", IS_PRODUCTION=True)
    def test_an_allowlist_alone_satisfies_the_check(self) -> None:
        self.assertEqual(check_metrics_endpoint_is_guarded(), [])

    @override_settings(UL_METRICS_ENABLED=True, UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="", IS_PRODUCTION=False)
    def test_a_local_checkout_may_leave_it_open(self) -> None:
        self.assertEqual(check_metrics_endpoint_is_guarded(), [])

    @override_settings(UL_METRICS_ENABLED=False, UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="", IS_PRODUCTION=True)
    def test_a_disabled_endpoint_needs_no_guard(self) -> None:
        self.assertEqual(check_metrics_endpoint_is_guarded(), [])

    def test_the_check_is_registered(self) -> None:
        # An unregistered check runs in this file and nowhere else.
        from django.core.checks import registry

        self.assertIn(check_metrics_endpoint_is_guarded, registry.registry.get_checks())


class MetricsInstrumentationGateTests(SimpleTestCase):
    """Who registers django-prometheus, and what happens when it is absent.

    ``UL_METRICS_ENABLED`` is an operator switch flipped on running deployments,
    independently of the image build that would carry the package. It reaches
    every process sharing the ``.env``, so both halves matter: only scraped
    processes should pay for the middleware, and a process that cannot import
    the package should say which setting asked for it.
    """

    def test_only_scraped_roles_are_instrumented(self) -> None:
        # Every role that appears in docker-compose.yml, so a new one added
        # there has to be considered here rather than silently instrumented.
        for role in ("websocket", "worker", "panels", "beat", "metrics", "sandbox", "inference", "ai"):
            with self.subTest(role=role):
                self.assertFalse(_metrics.instrumentation_wanted(metrics_enabled=True, process_role=role), f"{role!r} is never scraped; instrumenting it costs a middleware pair whose counters nothing reads.")

    def test_the_web_role_is_instrumented(self) -> None:
        self.assertTrue(_metrics.instrumentation_wanted(metrics_enabled=True, process_role="web"))

    def test_an_unspecified_role_is_instrumented(self) -> None:
        # What a local checkout, `runserver` and this suite report. Reading the
        # endpoint with curl while working on it is the point of enabling it.
        self.assertTrue(_metrics.instrumentation_wanted(metrics_enabled=True, process_role="unspecified"))

    def test_the_flag_still_gates_the_web_role(self) -> None:
        self.assertFalse(_metrics.instrumentation_wanted(metrics_enabled=False, process_role="web"))

    def test_a_missing_package_names_the_setting(self) -> None:
        # The failure being replaced: a bare ModuleNotFoundError for a module the
        # operator never heard of, crash-looping every Django process at once.
        with mock.patch.dict(sys.modules, {"django_prometheus": None}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                _metrics.require_django_prometheus()
        self.assertIn("UL_METRICS_ENABLED", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ImportError)

    def test_an_installed_package_is_accepted(self) -> None:
        self.assertIsNone(_metrics.require_django_prometheus())

    def test_the_derived_setting_exists(self) -> None:
        # override_settings invents any name it is given; the middleware block in
        # base.py reads this one, so a suite that only overrode it would pass
        # against a settings module that never defined it.
        self.assertTrue(hasattr(settings, "UL_METRICS_INSTRUMENTED"))
        self.assertFalse(settings.UL_METRICS_INSTRUMENTED)

    def test_every_django_prometheus_registration_is_behind_the_role_gate(self) -> None:
        # Not the flag: gating these on UL_METRICS_ENABLED alone is what made a
        # worker import a package it does not ship. Each registration line is
        # matched to the `if` that most recently opened above it.
        base = (REPO_ROOT / "src/urbanlens/UrbanLens/settings/base.py").read_text().splitlines()
        gate: str | None = None
        registrations = 0
        for line in base:
            opened = re.match(r"^if (.+):$", line)
            if opened:
                gate = opened.group(1)
            elif line and not line[0].isspace():
                gate = None
            if "django_prometheus" not in line or line.lstrip().startswith("#"):
                continue
            self.assertEqual(gate, "UL_METRICS_INSTRUMENTED", f"{line.strip()!r} is registered under {gate!r}, which does not account for UL_PROCESS_ROLE.")
            # The quote is what separates a registration from the import guard
            # that shares the name (`_metrics.require_django_prometheus()`).
            registrations += '"django_prometheus' in line
        self.assertEqual(registrations, 3, "Expected the INSTALLED_APPS entry and both middlewares; adjust this count deliberately.")


class MetricsDeploymentWiringTests(SimpleTestCase):
    """The parts of this that live outside Python and cannot be unit-tested."""

    def test_multiproc_dir_is_not_a_shared_volume(self) -> None:
        # The failure this prevents: prometheus_client sums every .db file in
        # the directory into one scrape, so a directory shared between services
        # reports app, app-ws and the celery workers as whichever one was
        # scraped. Every path in the entrypoint's chown loop IS a shared named
        # volume, which is why this one is handled separately.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        multiproc = compose["services"]["app"]["environment"]["PROMETHEUS_MULTIPROC_DIR"]
        mounted = {mount.split(":")[1] for service in compose["services"].values() for mount in service.get("volumes", []) if isinstance(mount, str) and ":" in mount}
        self.assertNotIn(multiproc, mounted, f"{multiproc} is a mounted volume; metrics from different services would be blended into one scrape.")

    def test_entrypoint_creates_and_clears_the_directory(self) -> None:
        # Creating it is not enough. The files are keyed by pid and survive a
        # restart of the same container, so a stale one is summed into every
        # later scrape.
        entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text()
        self.assertIn("mkdir -p \"${PROMETHEUS_MULTIPROC_DIR}\"", entrypoint)
        self.assertRegex(entrypoint, r"rm -f \"\$\{PROMETHEUS_MULTIPROC_DIR\}\"/\*\.db")

    def test_gunicorn_retires_dead_workers(self) -> None:
        # Without child_exit -> mark_process_dead, a worker that exits leaves
        # its samples behind and every later scrape keeps reporting them as
        # though it were still serving.
        config = (REPO_ROOT / "gunicorn.conf.py").read_text()
        self.assertIn("def child_exit(", config)
        self.assertIn("mark_process_dead", config)

    def test_gunicorn_clears_the_directory_before_forking(self) -> None:
        config = (REPO_ROOT / "gunicorn.conf.py").read_text()
        self.assertIn("def on_starting(", config)

    def test_nginx_does_not_serve_metrics_publicly(self) -> None:
        # The outer layer. Django's own guard is the one that has to be right,
        # but the public vhost should not be the thing standing between a
        # misconfiguration and the internet.
        conf = (REPO_ROOT / "src/urbanlens/config/nginx/django.conf").read_text()
        self.assertRegex(conf, r"location\s*=\s*/metrics\s*\{[^}]*return\s+404", "The public vhost proxies /metrics to the app.")

    def test_each_service_that_could_serve_metrics_decides_explicitly(self) -> None:
        # Enabling it per service is the point, and every service that *could*
        # serve an endpoint has to say which way - including the ones saying no.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        declared = {name: (service.get("environment") or {}).get("UL_METRICS_ENABLED") for name, service in compose["services"].items() if "UL_METRICS_ENABLED" in (service.get("environment") or {})}
        self.assertEqual(declared.get("app"), "${UL_METRICS_ENABLED:-false}", "The web service should follow the env var, defaulting off.")
        self.assertEqual(declared.get("celery-metrics"), "${UL_METRICS_ENABLED:-false}", "The Celery exporter follows the same flag: it exits rather than binding an unguarded port, so deploying it while metrics are off is a restart loop.")
        self.assertEqual(declared.get("app-ws"), "false", "app-ws must pin this off explicitly - it shares .env with app, and environment: outranks env_file:, so merely omitting it would let a UL_METRICS_ENABLED meant for app enable a second endpoint here.")
        self.assertEqual(set(declared), {"app", "app-ws", "celery-metrics"})

    def test_every_service_sharing_the_env_file_decides_explicitly(self) -> None:
        # The trap this guards: `environment:` outranks `env_file:`, so a service
        # that reads .env and says nothing inherits whatever the operator set for
        # a different service. Only services that serve HTTP can expose the
        # endpoint, so only those have to decide.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        http_serving = {"app", "app-ws"}
        for name in http_serving:
            with self.subTest(service=name):
                self.assertIn("UL_METRICS_ENABLED", compose["services"][name]["environment"])

    def test_scrape_labels_follow_the_flag(self) -> None:
        # A container labelled prometheus_scrape=true with no endpoint behind it
        # is a scrape job that fails forever; the label has to track the flag
        # rather than being hardcoded true.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        app = compose["services"]["app"]
        self.assertEqual(app["labels"]["prometheus_scrape"], app["environment"]["UL_METRICS_ENABLED"])
        self.assertEqual(app["labels"]["prometheus_path"], "/metrics")

    def test_middleware_pair_is_outermost_and_innermost(self) -> None:
        # The pair only means anything in that order: the difference between the
        # two timers is what the rest of the stack costs.
        base = (REPO_ROOT / "src/urbanlens/UrbanLens/settings/base.py").read_text()
        self.assertRegex(base, r"MIDDLEWARE\.insert\(0,\s*[\"']django_prometheus\.middleware\.PrometheusBeforeMiddleware")
        self.assertRegex(base, r"MIDDLEWARE\.append\(\s*[\"']django_prometheus\.middleware\.PrometheusAfterMiddleware")

    def test_dependency_is_declared_and_locked(self) -> None:
        self.assertRegex((REPO_ROOT / "pyproject.toml").read_text(), r"django-prometheus~=")
        self.assertIn('name = "prometheus-client"', (REPO_ROOT / "uv.lock").read_text())

    def test_migration_export_is_off(self) -> None:
        # It runs a MigrationExecutor plan against the database on every scrape,
        # and /health/ready already reports migration state.
        self.assertFalse(settings.PROMETHEUS_EXPORT_MIGRATIONS)

    def test_latency_buckets_are_bounded_and_sorted(self) -> None:
        buckets = settings.PROMETHEUS_LATENCY_BUCKETS
        self.assertEqual(list(buckets), sorted(buckets), "Prometheus requires ascending histogram buckets.")
        self.assertEqual(buckets[-1], float("inf"))
        # Each bucket is a stored series per view and method; the default set is
        # finer than anything here resolves.
        self.assertLessEqual(len(buckets), 16)

    def test_a_bucket_matches_the_proxy_timeout(self) -> None:
        # Requests nginx gave up on should land in their own bucket rather than
        # being lumped into +Inf with everything else slow. The two numbers live
        # in different files, so the comment saying "keep these in step" is only
        # worth as much as this assertion.
        conf = (REPO_ROOT / "src/urbanlens/config/nginx/django.conf").read_text()
        match = re.search(r"location\s*/\s*\{[^}]*?proxy_read_timeout\s+(\d+)s", conf, re.S)
        self.assertIsNotNone(match, "Could not find proxy_read_timeout for the main location block.")
        timeout = float(match.group(1))
        self.assertIn(timeout, settings.PROMETHEUS_LATENCY_BUCKETS, f"nginx times out at {timeout}s but no latency bucket matches it; proxy-timed-out requests would be indistinguishable from merely slow ones.")


class CeleryQueueDepthCollectorTests(SimpleTestCase):
    """Queue depth, and the distinction between empty and unreachable."""

    def _samples(self, collector) -> dict:
        out = {}
        for family in collector.collect():
            for sample in family.samples:
                key = (sample.name, sample.labels.get("queue"))
                out[key] = sample.value
        return out

    def test_every_queue_is_reported(self) -> None:
        from urbanlens.dashboard.services.core.celery_metrics import CeleryQueueDepthCollector
        from urbanlens.dashboard.services.sandbox.queues import Queue

        collector = CeleryQueueDepthCollector()
        with mock.patch.object(CeleryQueueDepthCollector, "_read_depths", return_value={q.value: 3 for q in Queue}):
            samples = self._samples(collector)
        for queue in Queue:
            with self.subTest(queue=queue.value):
                self.assertEqual(samples[("urbanlens_celery_queue_depth", queue.value)], 3.0)
        self.assertEqual(samples[("urbanlens_celery_broker_up", None)], 1.0)

    def test_unreachable_broker_reports_down_and_emits_no_depths(self) -> None:
        # The failure this guards: publishing 0 for every queue when the broker
        # is unreachable reads as a healthy idle system on a dashboard, and
        # silences exactly the alert that should fire.
        from urbanlens.dashboard.services.core.celery_metrics import CeleryQueueDepthCollector

        collector = CeleryQueueDepthCollector()
        with mock.patch.object(CeleryQueueDepthCollector, "_read_depths", return_value=None):
            samples = self._samples(collector)
        self.assertEqual(samples[("urbanlens_celery_broker_up", None)], 0.0)
        depth_samples = [key for key in samples if key[0] == "urbanlens_celery_queue_depth"]
        self.assertEqual(depth_samples, [], "An unreachable broker must not publish zeroed depths.")

    def test_a_broker_error_is_swallowed_into_broker_down(self) -> None:
        # A raised exception here would fail the whole /metrics response, so a
        # Celery problem would become a total observability outage.
        from urbanlens.dashboard.services.core import celery_metrics

        with mock.patch.object(celery_metrics.current_app, "connection_for_read", side_effect=OSError("broker gone")):
            self.assertIsNone(celery_metrics.CeleryQueueDepthCollector()._read_depths())

    def test_label_values_come_from_the_queue_enum(self) -> None:
        # Cardinality is fixed by the code, not by anything a request influences.
        from urbanlens.dashboard.services.core.celery_metrics import CeleryQueueDepthCollector
        from urbanlens.dashboard.services.sandbox.queues import Queue

        self.assertEqual(set(CeleryQueueDepthCollector.QUEUES), set(Queue))

    def test_collector_is_attached_to_the_multiprocess_registry(self) -> None:
        # The failure being avoided: MultiProcessCollector reads only the
        # workers' sample files, so a collector left on the default registry
        # works under runserver and silently vanishes in production.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.dict(os.environ, {MULTIPROC_DIR_ENV: directory.name}):
            registry = _build_registry()
        names = {type(c).__name__ for c in registry._collector_to_names}
        self.assertIn("CeleryQueueDepthCollector", names)


class CeleryEventMetricsTests(SimpleTestCase):
    """Translating Celery's event stream into metrics."""

    def _metrics(self, max_task_labels=200):
        # A per-test registry only isolates because settings/test.py pops
        # PROMETHEUS_MULTIPROC_DIR: in multiprocess mode prometheus_client backs
        # every sample with an mmap keyed on metric name and labels, shared by
        # every registry in the process, and two tests touching task="other"
        # would see each other's increments.
        from prometheus_client import CollectorRegistry

        from urbanlens.dashboard.services.core.celery_events import CeleryEventMetrics

        return CeleryEventMetrics(registry=CollectorRegistry(), max_task_labels=max_task_labels)

    def _value(self, metrics, name, **labels):
        return metrics.registry.get_sample_value(name, labels) or 0.0

    def test_metric_values_are_isolated_per_registry(self) -> None:
        """Guards the isolation the assertions in this class depend on."""
        from prometheus_client import values

        self.assertEqual(values.ValueClass.__qualname__, "MutexValue", "tests are running in prometheus multiprocess mode, where counters bleed across registries")

    def test_succeeded_records_outcome_and_runtime(self) -> None:
        m = self._metrics()
        m.on_task_event({"type": "task-succeeded", "runtime": 2.5}, "urbanlens.tasks.real_task")
        self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task="urbanlens.tasks.real_task", state="succeeded"), 1.0)
        self.assertEqual(self._value(m, "urbanlens_celery_task_runtime_seconds_count", task="urbanlens.tasks.real_task"), 1.0)
        self.assertEqual(self._value(m, "urbanlens_celery_task_runtime_seconds_sum", task="urbanlens.tasks.real_task"), 2.5)

    def test_failed_records_outcome_but_no_runtime(self) -> None:
        # A failed task's duration is the time to the exception, which is not
        # comparable to a successful run - folding it in would drag the latency
        # percentiles toward whatever the failure happened to cost.
        m = self._metrics()
        m.on_task_event({"type": "task-failed", "runtime": 99.0}, "urbanlens.tasks.real_task")
        self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task="urbanlens.tasks.real_task", state="failed"), 1.0)
        self.assertEqual(self._value(m, "urbanlens_celery_task_runtime_seconds_count", task="urbanlens.tasks.real_task"), 0.0)

    def test_transitions_are_not_counted_as_outcomes(self) -> None:
        # task-received and task-started precede every task. Counting them
        # would double- or triple-count each one against its real outcome.
        m = self._metrics()
        for event_type in ("task-sent", "task-received", "task-started"):
            m.on_task_event({"type": event_type}, "urbanlens.tasks.real_task")
        total = sum(
            self._value(m, "urbanlens_celery_tasks_total", task="urbanlens.tasks.real_task", state=state)
            for state in ("succeeded", "failed", "rejected", "revoked", "retried")
        )
        self.assertEqual(total, 0.0)
        # ...but they are still counted as events, so the stream is observable.
        self.assertEqual(self._value(m, "urbanlens_celery_events_total", type="task-started"), 1.0)

    def test_real_task_names_are_reported_under_their_own_name(self) -> None:
        # The regression this replaced a registry allowlist to fix: the exporter
        # cannot afford to import the task registry, so an allowlist built from
        # it was empty and every real task collapsed into one useless bucket.
        m = self._metrics()
        for name in ("urbanlens.dashboard.tasks.sweep_achievements", "urbanlens.dashboard.tasks.run_scheduled_database_backup"):
            m.on_task_event({"type": "task-succeeded", "runtime": 1.0}, name)
            with self.subTest(name=name):
                self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task=name, state="succeeded"), 1.0)

    def test_task_names_past_the_cap_collapse_to_one_bucket(self) -> None:
        # Cardinality guard: an unbounded label set is how a Prometheus falls
        # over, so past the cap everything shares a series.
        from urbanlens.dashboard.services.core.celery_events import OVERFLOW

        m = self._metrics(max_task_labels=2)
        for name in ("task.a", "task.b", "task.c", "task.d"):
            m.on_task_event({"type": "task-succeeded", "runtime": 1.0}, name)

        for name in ("task.a", "task.b"):
            with self.subTest(admitted=name):
                self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task=name, state="succeeded"), 1.0)
        self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task=OVERFLOW, state="succeeded"), 2.0)
        for name in ("task.c", "task.d"):
            with self.subTest(rejected=name):
                self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task=name, state="succeeded"), 0.0)

    def test_a_name_already_admitted_keeps_its_series_after_the_cap(self) -> None:
        # The cap freezes the label set; it must not start dropping tasks that
        # were already being reported, which would make a series go silent
        # rather than simply not gain new ones.
        m = self._metrics(max_task_labels=1)
        for _ in range(3):
            m.on_task_event({"type": "task-succeeded", "runtime": 1.0}, "task.first")
        m.on_task_event({"type": "task-succeeded", "runtime": 1.0}, "task.second")
        self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task="task.first", state="succeeded"), 3.0)

    def test_a_none_task_name_does_not_crash(self) -> None:
        # Only task-received carries the name; an event arriving before it (or
        # after a restart dropped the state) resolves to None.
        from urbanlens.dashboard.services.core.celery_events import UNKNOWN

        m = self._metrics()
        m.on_task_event({"type": "task-succeeded", "runtime": 1.0}, None)
        self.assertEqual(self._value(m, "urbanlens_celery_tasks_total", task=UNKNOWN, state="succeeded"), 1.0)

    def test_a_missing_name_is_distinguishable_from_the_overflow_bucket(self) -> None:
        # Two different problems - "the worker never told us" and "we hit the
        # cap" - want different answers when reading the metric.
        from urbanlens.dashboard.services.core.celery_events import OVERFLOW, UNKNOWN

        self.assertNotEqual(UNKNOWN, OVERFLOW)

    def test_runtime_buckets_cover_the_task_time_limit(self) -> None:
        # CELERY_TASK_TIME_LIMIT is where a task is killed, so that boundary is
        # a bucket - otherwise "slow" and "hit the limit" land together in +Inf.
        from urbanlens.dashboard.services.core.celery_events import RUNTIME_BUCKETS

        self.assertIn(float(settings.CELERY_TASK_TIME_LIMIT), RUNTIME_BUCKETS)
        self.assertEqual(list(RUNTIME_BUCKETS), sorted(RUNTIME_BUCKETS))


class MetricsAuthSharedGateTests(SimpleTestCase):
    """The web view and the Celery exporter must use one gate, not two."""

    def test_controller_delegates_to_the_shared_gate(self) -> None:
        # The failure this prevents: a second copy of "is this scraper
        # authorized" drifting, so one endpoint ends up open and nothing says so.
        source = (REPO_ROOT / "src/urbanlens/dashboard/controllers/metrics.py").read_text()
        exporter = (REPO_ROOT / "src/urbanlens/dashboard/management/commands/celery_metrics_exporter.py").read_text()
        for name, text in (("controller", source), ("exporter", exporter)):
            with self.subTest(module=name):
                self.assertIn("from urbanlens.dashboard.services.core.metrics_auth import", text)
                self.assertNotIn("compare_digest", text, "The token comparison belongs in metrics_auth, not here.")

    @override_settings(UL_METRICS_TOKEN="tok", UL_METRICS_ALLOWED_CIDRS="")
    def test_token_gate(self) -> None:
        from urbanlens.dashboard.services.core.metrics_auth import token_ok

        self.assertTrue(token_ok("Bearer tok"))
        self.assertTrue(token_ok("bearer tok"))
        self.assertFalse(token_ok("Bearer to"))
        self.assertFalse(token_ok("Basic tok"))
        self.assertFalse(token_ok(""))

    @override_settings(UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="10.2.0.0/24")
    def test_network_gate(self) -> None:
        from urbanlens.dashboard.services.core.metrics_auth import network_ok

        self.assertTrue(network_ok("10.2.0.9"))
        self.assertFalse(network_ok("203.0.113.7"))
        self.assertFalse(network_ok("garbage"))

    @override_settings(UL_METRICS_TOKEN="", UL_METRICS_ALLOWED_CIDRS="")
    def test_gates_configured_is_false_when_both_empty(self) -> None:
        from urbanlens.dashboard.services.core.metrics_auth import gates_configured

        self.assertFalse(gates_configured())


class CeleryMetricsExporterServiceTests(SimpleTestCase):
    """The exporter service's deployment shape."""

    def test_exporter_port_is_not_published(self) -> None:
        # It must be reachable only on the compose network; the scraping agent
        # joins that network rather than the port being exposed to the host.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        service = compose["services"]["celery-metrics"]
        self.assertNotIn("ports", service, "celery-metrics must not publish its port to the host.")
        self.assertEqual(service["labels"]["prometheus_port"], "8002")
        self.assertEqual(service["labels"]["prometheus_scrape"], service["environment"]["UL_METRICS_ENABLED"])

    def test_worker_task_events_follow_the_metrics_flag(self) -> None:
        # Events cost a broker publish per task transition; a deployment not
        # collecting metrics should not pay for them.
        self.assertEqual(settings.CELERY_WORKER_SEND_TASK_EVENTS, settings.UL_METRICS_ENABLED)


class MetricsScrapeTargetHostTests(SimpleTestCase):
    """The scrape target has to be a name Django will accept as a Host."""

    def test_app_has_a_hostname_legal_network_alias(self) -> None:
        # Found the hard way on a real deploy: Prometheus/Alloy derive the Host
        # header from __address__, and Django validates Host against a regex
        # that has no underscore in it *before* consulting ALLOWED_HOSTS. So
        # scraping the `urbanlens_app` alias returns 400 for every request -
        # correct token, correct labels, correct network, no clue why.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        aliases = compose["services"]["app"]["networks"]["app_network"]["aliases"]
        legal = [a for a in aliases if "_" not in a]
        self.assertTrue(legal, f"app needs a network alias that is a legal hostname; got {aliases}. A scraper cannot use an underscored one.")

    def test_django_really_rejects_the_underscored_alias(self) -> None:
        # Pins the reason the alias above exists, so nobody 'simplifies' it away.
        from django.http.request import split_domain_port

        self.assertEqual(split_domain_port("urbanlens_app:8000"), ("", ""))
        self.assertEqual(split_domain_port("urbanlens-app:8000"), ("urbanlens-app", "8000"))
