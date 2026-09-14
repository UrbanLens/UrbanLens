"""nginx must re-resolve the app's address, not keep the one it saw at startup (P7).

A literal hostname in `proxy_pass` is resolved once, when the config loads. A recreated
`app` container comes back on a new IP, and nginx keeps dialling the old one: every
request 502s while every container reports healthy, until nginx itself is restarted.
A variable in `proxy_pass` is resolved per request through `resolver`, with its TTL.

These read the config files; they assert the wiring, not the behaviour.
"""

from __future__ import annotations

from urbanlens.core.tests.nginx_config import NGINX_DIR, directive_arguments
from urbanlens.core.tests.testcase import SimpleTestCase

_PROXYING_CONFIGS = ("django.conf", "media.conf.template")


class NginxResolvesUpstreamsPerRequestTests(SimpleTestCase):
    def test_every_proxy_pass_names_its_upstream_through_a_variable(self) -> None:
        literal = [
            f"{name}: proxy_pass {' '.join(arguments)}"
            for name in _PROXYING_CONFIGS
            for arguments in directive_arguments((NGINX_DIR / name).read_text(), "proxy_pass")
            if "$" not in arguments[0]
        ]

        self.assertEqual(literal, [], "a literal upstream is resolved once, at config load")

    def test_every_proxying_config_declares_a_resolver(self) -> None:
        for name in _PROXYING_CONFIGS:
            with self.subTest(config=name):
                text = (NGINX_DIR / name).read_text()
                self.assertTrue(
                    directive_arguments(text, "proxy_pass"),
                    "guards the check from passing on a file that proxies nothing",
                )
                self.assertTrue(
                    directive_arguments(text, "resolver"), "a variable proxy_pass with no resolver fails every request"
                )
