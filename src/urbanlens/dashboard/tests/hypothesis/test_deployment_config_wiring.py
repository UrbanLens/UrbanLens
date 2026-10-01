"""Deployment files agree with the settings that now refuse to start without them."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _flags(command: list[str]) -> dict[str, str]:
    return dict(part.lstrip("-").partition("=")[::2] for part in command if part.startswith("--"))


def _default(value: str) -> str:
    """`${NAME:-default}` -> default; a plain value unchanged."""
    return value.partition(":-")[2].rstrip("}") if value.startswith("${") else value


class TheChannelLayerIsProvisionedTests(SimpleTestCase):
    """G4-21: the Channels layer shared a raise-on-full Dragonfly with the cache."""

    def test_compose_runs_an_instance_for_it(self) -> None:
        services = _services()
        self.assertEqual(services["channel-layer"]["image"], services["dragonfly"]["image"])

    def test_it_never_evicts(self) -> None:
        """Evicting a Channels key breaks a live socket, so it refuses like the shared store does."""
        self.assertNotIn("cache_mode", _flags(_services()["channel-layer"]["command"]))

    def test_dragonfly_would_start_with_its_memory(self) -> None:
        """Dragonfly refuses to start below 256MiB per thread."""
        flags = _flags(_services()["channel-layer"]["command"])
        threads = int(_default(flags["proactor_threads"]))
        self.assertGreaterEqual(int(_default(flags["maxmemory"]).removesuffix("mb")), 256 * threads)

    def test_the_tiers_that_send_and_consume_are_told_where_it_is(self) -> None:
        services = _services()
        alias = services["channel-layer"]["networks"]["app_network"]["aliases"][0]
        for name in ("app", "app-ws", "celery-worker"):
            with self.subTest(service=name):
                self.assertIn(alias, services[name]["environment"]["UL_CHANNEL_LAYER_URL"])
                self.assertEqual(services[name]["depends_on"]["channel-layer"]["condition"], "service_healthy")

    def test_it_is_reachable_from_every_service_told_about_it(self) -> None:
        services = _services()
        joined = set(services["channel-layer"]["networks"])
        told = {
            name: set(service.get("networks") or ())
            for name, service in services.items()
            if "UL_CHANNEL_LAYER_URL" in (service.get("environment") or {})
        }

        self.assertTrue(told)
        for name, networks in told.items():
            with self.subTest(service=name):
                self.assertTrue(networks & joined, f"{name} is pointed at it on none of {sorted(networks)}")


class TheImageBuildSatisfiesTheSettingsTests(SimpleTestCase):
    """The build runs collectstatic under UL_ENVIRONMENT=production, which imports the settings that now refuse
    a deployment missing a store, a broker or a site URL."""

    def _build_step_environment(self) -> dict[str, str]:
        text = DOCKERFILE.read_text(encoding="utf-8")
        step = re.search(
            r"^RUN ((?:[A-Z_]+=\S+ \\\n\s*)+)gosu appuser python /app/src/bin/init.py --frontend-only",
            text,
            re.MULTILINE,
        )
        if step is None:
            self.fail("the build-time frontend step has moved")
        return dict(re.findall(r"([A-Z_]+)=(\S+)", step.group(1)))

    def test_the_build_step_environment_imports_the_settings(self) -> None:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "UL_ENVIRONMENT": "production",
            # Blank so a checkout's .env cannot fill it in; the build has none.
            "DJANGO_DEBUG": "",
            **self._build_step_environment(),
        }
        result = subprocess.run(
            [sys.executable, "-c", "import urbanlens.UrbanLens.settings.base"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
