"""`UL_SITE_URL` reaches every tier, and nothing but the settings fallback supplies a local one."""

from __future__ import annotations

import os
import pathlib
import re

from django.conf import settings
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

#: The repo root - parents[5] from src/urbanlens/dashboard/tests/hypothesis/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_SETTINGS_PATH = pathlib.Path(__file__).resolve().parents[3] / "UrbanLens" / "settings" / "base.py"

#: `${NAME:-default}`, capturing the default. One level of `${...}` nesting is allowed inside it.
_DEFAULT = r"\$\{%s:-((?:[^{}]|\$\{[^}]*\})*)\}"


def _defaults(name: str) -> set[str]:
    """Every default `docker-compose.yml` gives for one variable."""
    return set(re.findall(_DEFAULT % re.escape(name), _COMPOSE_PATH.read_text(encoding="utf-8")))


class ComposeSiteUrlTests(SimpleTestCase):
    """Compose defaulted it to http://localhost:<port>, so a deployment that forgot it never saw the old warning."""

    def test_the_app_port_has_exactly_one_default(self) -> None:
        self.assertEqual(len(_defaults("UL_APP_PORT")), 1, _defaults("UL_APP_PORT"))

    def test_no_service_is_handed_an_origin_it_was_not_configured_with(self) -> None:
        self.assertEqual({value for value in _defaults("UL_SITE_URL") if "://" in value}, set())

    def test_every_django_process_is_passed_it(self) -> None:
        """An empty value reads as unset, which refuses to start in a deployment. A tier left out of the pass-through
        (beat has no env_file) would be the one that could not start."""
        services = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))["services"]
        django_tiers = {
            name: service["environment"]
            for name, service in services.items()
            if "UL_ENVIRONMENT" in (service.get("environment") or {})
        }

        self.assertIn("celery-beat", django_tiers)
        for name, environment in django_tiers.items():
            with self.subTest(service=name):
                self.assertIn("UL_SITE_URL", environment)

    def test_the_media_host_fails_closed_rather_than_guessing_an_origin(self) -> None:
        # `MEDIA_FRAME_ANCESTORS: ${UL_SITE_URL:-'none'}`: a deployment nobody configured refuses framing.
        self.assertIn("'none'", _defaults("UL_SITE_URL"))


class SettingsSiteUrlPortTests(SimpleTestCase):
    """The fallback local, development and testing use when `UL_SITE_URL` is unset."""

    def test_the_site_url_fallback_is_not_a_literal_port(self) -> None:
        source = _SETTINGS_PATH.read_text(encoding="utf-8")
        fallback = re.search(r"^_SITE_URL_FALLBACK = ([^\n]+)$", source, re.MULTILINE)

        if fallback is None:
            self.fail("no _SITE_URL_FALLBACK assignment - has this moved?")
        self.assertIn("_APP_PORT", fallback.group(1), "the fallback names a port independently of UL_APP_PORT")

    def test_no_cors_origin_is_minted_from_a_hardcoded_port(self) -> None:
        # These lists feed CORS_ALLOWED_ORIGINS and CSRF_TRUSTED_ORIGINS.
        source = _SETTINGS_PATH.read_text(encoding="utf-8")
        listed = re.findall(r"^\s*domains = \[(.*?)\]", source, re.MULTILINE | re.DOTALL)

        self.assertTrue(listed, "no `domains = [...]` found - has this moved?")
        for block in listed:
            with self.subTest(block=" ".join(block.split())[:60]):
                ports = re.findall(r"(?:localhost|127\.0\.0\.1):(\d+)", block)
                self.assertEqual(
                    ports,
                    [port for port in ports if port == "8000"],
                    f"a port literal other than runserver's 8000: {ports}",
                )

    def test_the_fallback_resolves_to_the_configured_port(self) -> None:
        self.assertTrue(
            settings.SITE_URL.endswith(f":{os.getenv('UL_APP_PORT', '21800')}") or os.getenv("UL_SITE_URL"),
            settings.SITE_URL,
        )
