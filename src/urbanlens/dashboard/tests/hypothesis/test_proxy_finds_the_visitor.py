"""nginx must find the visitor's address whichever Docker network its front door arrives from.

When the hop in front of nginx is not trusted, `X-Forwarded-For` is ignored and every visitor has the front door's
address. Every per-address limit then becomes a site-wide one: `limit_conn ws_conn 60` stops the sixty-first
notification socket on the whole site.
"""

from __future__ import annotations

import ipaddress

from django.test import SimpleTestCase

from urbanlens.core.tests.nginx_config import NGINX_DIR, directive_arguments

#: The daemon's default ``default-address-pools``: 172.17-31 as /16s, then 192.168.0.0/16 carved into /20s once
#: those run out. Which one a stack lands on depends on how many networks the host already has.
DOCKER_DEFAULT_POOLS = [ipaddress.ip_network(f"172.{octet}.0.0/16") for octet in range(17, 32)] + [
    ipaddress.ip_network("192.168.0.0/16")
]

#: Where a hop nginx trusts may sit. A trusted range a visitor can send from lets them choose their own address.
PROXY_SPACE = [ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16")]

CONFIGS = ("django.conf", "media.conf.template")


def _trusted(name: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    text = (NGINX_DIR / name).read_text(encoding="utf-8")
    return [ipaddress.ip_network(arguments[0]) for arguments in directive_arguments(text, "set_real_ip_from")]


class TheProxyFindsTheVisitorTests(SimpleTestCase):
    """Real-IP trust covers every network Docker can hand the stack, and nothing else."""

    def test_every_config_trusts_some_hop(self) -> None:
        for name in CONFIGS:
            with self.subTest(config=name):
                self.assertTrue(_trusted(name), f"{name} trusts no hop, so X-Forwarded-For is never read")

    def test_every_docker_default_pool_is_trusted(self) -> None:
        """A stack on a 192.168 network - chiron's perf environment is one - loses every visitor's address."""
        for name in CONFIGS:
            trusted = _trusted(name)
            for pool in DOCKER_DEFAULT_POOLS:
                with self.subTest(config=name, pool=str(pool)):
                    self.assertTrue(
                        any(pool.version == network.version and pool.subnet_of(network) for network in trusted),
                        f"{name} does not trust {pool}; a front door on that network is every visitor at once",
                    )

    def test_no_range_a_visitor_sends_from_is_trusted(self) -> None:
        for name in CONFIGS:
            for network in _trusted(name):
                with self.subTest(config=name, network=str(network)):
                    self.assertTrue(
                        any(network.version == space.version and network.subnet_of(space) for space in PROXY_SPACE),
                        f"{name} trusts {network}, outside the Docker pools, so a visitor there picks their own address",
                    )
