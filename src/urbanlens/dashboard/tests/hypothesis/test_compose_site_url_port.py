"""`UL_SITE_URL`'s default names a port nothing serves."""

from __future__ import annotations

import os
import pathlib
import re

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase

#: The repo root - parents[5] from src/urbanlens/dashboard/tests/hypothesis/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_SETTINGS_PATH = pathlib.Path(__file__).resolve().parents[3] / "UrbanLens" / "settings" / "base.py"

#: `${NAME:-default}`, capturing the default. One level of `${...}` nesting is
#: allowed inside it, which is how UL_SITE_URL borrows the app's port.
_DEFAULT = r"\$\{%s:-((?:[^{}]|\$\{[^}]*\})*)\}"


def _defaults(name: str) -> set[str]:
    """Every default `docker-compose.yml` gives for one variable."""
    return set(re.findall(_DEFAULT % re.escape(name), _COMPOSE_PATH.read_text(encoding="utf-8")))


def _url_defaults(name: str) -> set[str]:
    """Only the defaults that are an origin, not a refusal like `'none'`."""
    return {value for value in _defaults(name) if "://" in value}


class ComposeSiteUrlPortTests(SimpleTestCase):
    """The port the app is published on, and the port it is told it lives on."""

    def test_the_app_port_has_exactly_one_default(self) -> None:
        self.assertEqual(len(_defaults("UL_APP_PORT")), 1, _defaults("UL_APP_PORT"))

    def test_site_url_borrows_the_port_rather_than_restating_it(self) -> None:
        # Stronger than "the two literals match": a restated literal is what
        # drifted in the first place, and matching literals would pass again the
        # next time one of them moved.
        for site_url in _url_defaults("UL_SITE_URL"):
            with self.subTest(site_url=site_url):
                self.assertIn(
                    "${UL_APP_PORT",
                    site_url,
                    f"UL_SITE_URL defaults to {site_url}, which names a port independently of UL_APP_PORT",
                )

    def test_the_ports_agree_once_expanded(self) -> None:
        app_port = next(iter(_defaults("UL_APP_PORT")))

        for site_url in _url_defaults("UL_SITE_URL"):
            with self.subTest(site_url=site_url):
                expanded = re.sub(r"\$\{UL_APP_PORT:-([^}]*)\}", r"\1", site_url)
                self.assertTrue(
                    expanded.endswith(f":{app_port}"),
                    f"{site_url} expands to {expanded}, but the app is published on {app_port}",
                )

    def test_every_service_that_builds_urls_agrees_on_the_default(self) -> None:
        self.assertEqual(len(_url_defaults("UL_SITE_URL")), 1, _url_defaults("UL_SITE_URL"))

    def test_the_media_host_fails_closed_rather_than_guessing_an_origin(self) -> None:
        # `MEDIA_FRAME_ANCESTORS: ${UL_SITE_URL:-'none'}` is the one place the
        # default is deliberately not a URL: a deployment nobody configured
        # should refuse framing rather than allow it from a guess.
        self.assertIn("'none'", _defaults("UL_SITE_URL"))


class SettingsSiteUrlPortTests(SimpleTestCase):
    """Django's own fallback, which `docker-compose.yml` does not reach.

    Fixing the compose default alone left `settings/base.py` restating the same stale literal - and that one is
    what any process started without `UL_SITE_URL` in its environment actually uses to build absolute links."""

    def test_the_site_url_fallback_is_not_a_literal_port(self) -> None:
        source = _SETTINGS_PATH.read_text(encoding="utf-8")
        fallback = re.search(r"SITE_URL = _site_url_env or ([^\n]+)", source)

        self.assertIsNotNone(fallback)
        self.assertIn("_APP_PORT", fallback.group(1), "the fallback names a port independently of UL_APP_PORT")

    def test_no_cors_origin_is_minted_from_a_hardcoded_port(self) -> None:
        # These lists feed CORS_ALLOWED_ORIGINS and CSRF_TRUSTED_ORIGINS.
        source = _SETTINGS_PATH.read_text(encoding="utf-8")
        listed = re.findall(r"^\s*domains = \[(.+?)\]", source, re.MULTILINE | re.DOTALL)

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
