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
import tempfile
from unittest import mock

from django.conf import settings
from django.core.checks import Error
from django.test import RequestFactory, override_settings
from django.urls import NoReverseMatch, reverse
import prometheus_client
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
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

    def test_only_the_web_service_opts_in(self) -> None:
        # Enabling it per service is the point: app-ws serves only /ws/, so its
        # HTTP metrics would be empty, and a worker has no HTTP surface at all.
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        declared = {name: (service.get("environment") or {}).get("UL_METRICS_ENABLED") for name, service in compose["services"].items() if "UL_METRICS_ENABLED" in (service.get("environment") or {})}
        self.assertEqual(declared.get("app"), "${UL_METRICS_ENABLED:-false}", "The web service should follow the env var, defaulting off.")
        self.assertEqual(declared.get("app-ws"), "false", "app-ws must pin this off explicitly - it shares .env with app, and environment: outranks env_file:, so merely omitting it would let a UL_METRICS_ENABLED meant for app enable a second endpoint here.")
        self.assertEqual(set(declared), {"app", "app-ws"})

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
