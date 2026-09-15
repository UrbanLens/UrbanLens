"""nginx must hold every visitor's connections at the capacity target, or it refuses them before the app is asked.

A proxied request costs two connections, the visitor's and the upstream's, and each is a file descriptor. The
notification socket holds its pair for as long as the page is open, so the site's concurrent-user ceiling is
set here first. The container's soft descriptor limit is 1024 whatever ``worker_connections`` says.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from urbanlens.core.tests.nginx_config import NGINX_DIR, directive_arguments

#: D15's eventual target. Connections cost nginx a few kilobytes each, so there is no reason to size it for less.
CONCURRENT_USERS = 10_000

#: The notification socket's pair, held for the life of the page, and a pair for a request in flight beside it.
CONNECTIONS_PER_USER = 2 * 2


def _single(name: str) -> int:
    text = (NGINX_DIR / "nginx.conf").read_text(encoding="utf-8")
    values = directive_arguments(text, name)
    if len(values) != 1:
        raise AssertionError(f"nginx.conf sets {name} {len(values)} times; expected once")
    return int(values[0][0])


class TheProxyHoldsTheCrowdTests(SimpleTestCase):
    """nginx's connection and descriptor ceilings cover the capacity target."""

    def test_the_workers_hold_every_users_connections(self) -> None:
        capacity = _single("worker_processes") * _single("worker_connections")

        self.assertGreaterEqual(capacity, CONCURRENT_USERS * CONNECTIONS_PER_USER)

    def test_each_worker_may_open_a_descriptor_per_connection(self) -> None:
        """Without this a worker stops accepting at 1024 descriptors, about 500 proxied sockets."""
        self.assertGreaterEqual(_single("worker_rlimit_nofile"), 2 * _single("worker_connections"))
