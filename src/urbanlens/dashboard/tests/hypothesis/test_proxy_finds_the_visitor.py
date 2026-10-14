"""
nginx must find the visitor's address whichever Docker network its front door arrives from.

When the hop in front of nginx is not trusted, `X-Forwarded-For` is ignored and every visitor has the front door's
address. Every per-address limit then becomes a site-wide one: `limit_conn ws_conn 60` stops the sixty-first
notification socket on the whole site.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import subprocess
import tempfile

from django.test import SimpleTestCase

from urbanlens.core.tests.nginx_config import NGINX_DIR, REPO_ROOT

#: The daemon's default ``default-address-pools``: 172.17-31 as /16s, then 192.168.0.0/16 carved into /20s once
#: those run out. Which one a stack lands on depends on how many networks the host already has.
DOCKER_DEFAULT_POOLS = [ipaddress.ip_network(f"172.{octet}.0.0/16") for octet in range(17, 32)] + [
    ipaddress.ip_network("192.168.0.0/16")
]

#: Where a hop nginx trusts may sit. A trusted range a visitor can send from lets them choose their own address.
PROXY_SPACE = [ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16")]

CONFIGS = ("django.conf.template", "media.conf.template")


SCRIPT = NGINX_DIR / "10-trusted-proxies.sh"


def _trusted(name: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The ranges a vhost trusts by default: its include is written by the script from the script's default list."""
    text = (NGINX_DIR / name).read_text(encoding="utf-8")
    assert "include /etc/nginx/conf.d/trusted_proxies.inc;" in text, f"{name} does not include the trusted-proxy list"
    default = re.search(r'^DEFAULT_TRUSTED_PROXY_CIDRS="([^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert default is not None
    return [ipaddress.ip_network(cidr) for cidr in default.group(1).split()]


def _run_script(cidrs: str | None) -> tuple[int, str]:
    """Run the entrypoint script against a scratch output file; return its exit code and the file's text."""
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "trusted_proxies.inc"
        script = Path(directory) / "run.sh"
        script.write_text(SCRIPT.read_text(encoding="utf-8").replace("/etc/nginx/conf.d/trusted_proxies.inc", str(out)))
        env = {k: v for k, v in os.environ.items() if k != "UL_TRUSTED_PROXY_CIDRS"}
        if cidrs is not None:
            env["UL_TRUSTED_PROXY_CIDRS"] = cidrs
        result = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True, check=False)  # noqa: S603, S607
        return result.returncode, out.read_text(encoding="utf-8") if out.exists() else ""


class TheProxyFindsTheVisitorTests(SimpleTestCase):
    """Real-IP trust covers every network Docker can hand the stack, and nothing else."""

    def test_every_config_trusts_some_hop(self) -> None:
        for name in CONFIGS:
            with self.subTest(config=name):
                self.assertTrue(_trusted(name), f"{name} trusts no hop, so X-Forwarded-For is never read")

    def test_every_docker_default_pool_is_trusted(self) -> None:
        """A stack on a 192.168 network loses every visitor's address."""
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


class TrustedProxyCidrsTests(SimpleTestCase):
    """UL_TRUSTED_PROXY_CIDRS replaces the default list; the script refuses what is not an address."""

    def test_unset_and_blank_keep_the_defaults(self) -> None:
        for value in (None, "", "  "):
            with self.subTest(value=value):
                code, text = _run_script(value)
                self.assertEqual(code, 0)
                self.assertEqual(text, "set_real_ip_from 172.16.0.0/12;\nset_real_ip_from 192.168.0.0/16;\n")

    def test_space_and_comma_separated_lists_are_accepted(self) -> None:
        for value in ("10.0.0.0/8 203.0.113.5", "10.0.0.0/8,203.0.113.5", "10.0.0.0/8, 203.0.113.5"):
            with self.subTest(value=value):
                code, text = _run_script(value)
                self.assertEqual(code, 0)
                self.assertEqual(text, "set_real_ip_from 10.0.0.0/8;\nset_real_ip_from 203.0.113.5;\n")

    def test_ipv6_is_accepted(self) -> None:
        self.assertEqual(_run_script("fd00::/8")[1], "set_real_ip_from fd00::/8;\n")

    def test_config_injection_is_refused(self) -> None:
        for value in ("10.0.0.1; root /", "10.0.0.1\nroot /;", "$host"):
            with self.subTest(value=value):
                self.assertNotEqual(_run_script(value)[0], 0)

    def test_compose_passes_the_variable_and_the_script_to_both_nginx_services(self) -> None:
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual(compose.count("UL_TRUSTED_PROXY_CIDRS: ${UL_TRUSTED_PROXY_CIDRS:-}"), 2)
        self.assertEqual(compose.count("10-trusted-proxies.sh:/docker-entrypoint.d/10-trusted-proxies.sh:ro"), 2)
