"""The port in an absolute URL is the one the site is published on, never one the client names.

Django runs with ``USE_X_FORWARDED_HOST`` and validates only the domain half of that header against
``ALLOWED_HOSTS``. nginx's raw ``$http_host`` therefore let any client stamp its own port onto every emailed
invite, verification link, OAuth ``redirect_uri`` and WebAuthn origin built from its request.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase
import yaml

from urbanlens.core.tests.nginx_config import NGINX_DIR, REPO_ROOT, directive_arguments, parsed_directives

VHOST = "django.conf.template"
PUBLISHED_PORT = "21810"


def _vhost() -> str:
    return (NGINX_DIR / VHOST).read_text(encoding="utf-8")


def _forwarded_host_variables() -> list[str]:
    return [
        arguments[1]
        for arguments in directive_arguments(_vhost(), "proxy_set_header")
        if arguments[0] == "X-Forwarded-Host"
    ]


def _map_entries(variable: str) -> dict[str, str]:
    """The ``map $http_host <variable>`` block's entries, keyed by match."""
    entries = {}
    for context, tokens, _ in parsed_directives(_vhost()):
        if context and context[-1] == ("map", "$http_host", variable):
            entries[tokens[0]] = tokens[1]
    return entries


def _forwards(variable: str, host_header: str) -> str:
    """What nginx forwards as X-Forwarded-Host for one Host header, once compose has rendered the template."""
    entries = _map_entries(variable)
    for match, value in entries.items():
        if match.startswith("~") and re.search(match[1:].replace("${UL_APP_PORT}", PUBLISHED_PORT), host_header):
            return host_header if value == "$http_host" else value
    return "$host"


class ForwardedHostPortIsTrustedTests(SimpleTestCase):
    """X-Forwarded-Host keeps the published port and drops any other."""

    def test_no_proxy_block_forwards_the_raw_host_header(self) -> None:
        variables = _forwarded_host_variables()
        self.assertTrue(variables, f"{VHOST} sets no X-Forwarded-Host, so a client could supply its own")
        self.assertNotIn("$http_host", variables, "the client's Host header, port and all, reaches Django unchecked")

    def test_only_the_published_port_survives(self) -> None:
        for variable in set(_forwarded_host_variables()):
            with self.subTest(variable=variable):
                self.assertEqual(
                    _map_entries(variable).get("default"),
                    "$host",
                    f"{variable} does not fall back to the port-less $host",
                )
                self.assertEqual(_forwards(variable, f"localhost:{PUBLISHED_PORT}"), f"localhost:{PUBLISHED_PORT}")
                self.assertEqual(_forwards(variable, "10.2.0.244:21810"), "10.2.0.244:21810")
                for spoofed in (
                    "localhost:1337",
                    f"localhost:{PUBLISHED_PORT}0",
                    f"localhost:1{PUBLISHED_PORT}",
                    "urbanlens.org",
                ):
                    with self.subTest(host=spoofed):
                        self.assertEqual(_forwards(variable, spoofed), "$host")

    def test_compose_renders_the_vhost_with_the_published_port(self) -> None:
        nginx = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["nginx"]
        self.assertIn(f"./src/urbanlens/config/nginx/{VHOST}:/etc/nginx/templates/{VHOST}:ro", nginx["volumes"])
        self.assertEqual(nginx.get("environment", {}).get("UL_APP_PORT"), "${UL_APP_PORT:-21800}")
        self.assertIn("${UL_APP_PORT:-21800}:8080", nginx["ports"])
