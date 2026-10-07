"""Every place UrbanLens starts names its ``UL_ENVIRONMENT``, and none of them names it production by default.

Since 2026-10-07 an unset or blank ``UL_ENVIRONMENT`` refuses to start (Jess's ruling). These pin the start-up
inventory that change was checked against, so a new compose tier or CI job cannot quietly rely on a default.
"""

from __future__ import annotations

import pathlib
import re

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes

#: The repo root - parents[5] from src/urbanlens/dashboard/tests/hypothesis/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE = _REPO_ROOT / "docker-compose.yml"

#: Built from the app image but never imports Django: ai-inference serves ``urbanlens_ai`` alone.
_NOT_DJANGO = frozenset({"ai-inference"})

#: What a tier may set it to: the operator's own value, passed through with no default, or a literal environment.
_PASSED_THROUGH = "${UL_ENVIRONMENT}"


def _services() -> dict[str, dict]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _environment(service: dict) -> dict[str, str]:
    """A service's ``environment:``, which compose accepts as a mapping or as ``NAME=value`` lines."""
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return dict(line.split("=", 1) if "=" in line else (line, "") for line in environment)
    return environment


def _django_tiers() -> dict[str, dict]:
    """Every compose service built from the repo's Dockerfile that runs Django."""
    return {
        name: service
        for name, service in _services().items()
        if isinstance(service.get("build"), dict)
        and service["build"].get("dockerfile") == "Dockerfile"
        and service["build"].get("context") == "."
        and name not in _NOT_DJANGO
    }


class ComposeTests(SimpleTestCase):
    def test_the_inventory_is_what_was_checked(self) -> None:
        """A new tier is read here first: does it run Django, and where does it get its environment?"""
        self.assertEqual(
            set(_django_tiers()),
            {
                "app",
                "app-ws",
                "db-setup",
                "celery-worker",
                "celery-worker-bulk",
                "celery-worker-panels",
                "media-worker",
                "media-worker-batch",
                "celery-beat",
                "celery-metrics",
                "ai-worker",
                "test-runner",
            },
        )

    def test_every_django_tier_is_passed_it_without_a_default(self) -> None:
        known = {str(name) for name in EnvironmentTypes}
        for name, service in _django_tiers().items():
            with self.subTest(service=name):
                value = _environment(service).get("UL_ENVIRONMENT")
                self.assertIn(value, {_PASSED_THROUGH, *known})

    def test_no_runtime_value_defaults_to_production(self) -> None:
        """``${UL_ENVIRONMENT:-production}`` was the compose half of "unset means production".

        Container names and the image's build argument still fall back to it: a name is not a policy, and the build
        argument picks the image's dependencies (production's are the smaller set), not the environment it runs as.
        """
        for name, service in _services().items():
            value = _environment(service).get("UL_ENVIRONMENT")
            if value is None:
                continue
            with self.subTest(service=name):
                self.assertNotRegex(str(value), r":-")


class ImageTests(SimpleTestCase):
    def test_the_image_bakes_no_runtime_environment(self) -> None:
        """``ARG UL_ENVIRONMENT=production`` exists only while building; an ``ENV`` would outlive it into every run."""
        dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        env_blocks = re.findall(r"^ENV\b(?:.*\\\n)*.*$", dockerfile, flags=re.MULTILINE)
        self.assertTrue(env_blocks)
        for block in env_blocks:
            self.assertNotIn("UL_ENVIRONMENT", block)


class ContinuousIntegrationTests(SimpleTestCase):
    def test_ci_names_the_test_suite_s_environment(self) -> None:
        workflow = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.assertEqual(workflow["env"]["UL_ENVIRONMENT"], "testing")
