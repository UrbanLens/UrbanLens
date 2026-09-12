"""The egress proxy's own healthcheck must not flood its log with errors.

`nc -z` opens a TCP connection and closes it without sending anything, so
tinyproxy logs `ERROR ... read_request_line: Client closed socket before read.`
every 30 seconds - 2,880 error lines a day, against a json-file driver holding
200 KB x 10. Real errors from this container age out within days.

That matters more here than the volume suggests. The egress proxy is the only
network boundary the AI tier has (docs/AI_PIPELINE.md), so its log is where a
blocked or failing provider call shows up, and it is the log least able to
afford noise. A component that cries wolf on a timer is one nobody reads.

Sending a complete request line instead is also a strictly better liveness
check: a `403 Filtered` response proves tinyproxy accepted the connection, read
and parsed the request, consulted the allowlist and wrote a response. `nc -z`
proves a port is open, which a wedged process also manages.

`tinyproxy.stats` is deliberately *not* in the allowlist - the healthcheck wants
the refusal. Adding a non-provider host to `config/egress/filter` to make this
return 200 would put a hole in a security boundary to satisfy a probe.
"""

from __future__ import annotations

import pathlib

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _service(name: str) -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"][name]


class TheHealthcheckCompletesARequestTests(SimpleTestCase):
    """A probe that never sends a request line is the thing being removed."""

    def test_the_probe_sends_a_request(self) -> None:
        probe = " ".join(_service("egress-proxy")["healthcheck"]["test"])

        self.assertIn("HTTP/1.0", probe, "the probe does not send a request line, so tinyproxy logs an error for it")

    def test_the_probe_is_not_a_bare_connect(self) -> None:
        probe = " ".join(_service("egress-proxy")["healthcheck"]["test"])

        self.assertNotIn("nc -z", probe)

    def test_the_probe_asks_for_a_host_the_allowlist_refuses(self) -> None:
        """The refusal is the point: a probe aimed at a permitted provider would
        send real traffic to that provider every 30 seconds."""
        probe = " ".join(_service("egress-proxy")["healthcheck"]["test"])
        allowlist = (REPO_ROOT / "src" / "urbanlens" / "config" / "egress" / "filter").read_text(encoding="utf-8")

        self.assertIn("tinyproxy.stats", probe)
        self.assertNotIn(
            "tinyproxy.stats", allowlist, "the probe's host was added to the egress allowlist to make it pass"
        )
