"""The nginx config must be one nginx will actually load.

`limit_conn_zone` was added to `nginx.conf` inside `events {}` rather than
`http {}`. Every guard passed: the zone was declared, the `/ws/` location
applied it, and real_ip preceded it. All three were `assertIn` against
whole-file text, which cannot see *which block* a directive landed in - so the
suite stayed green while nginx refused to start at all, taking the whole site
with it (N22 H59).

The failure mode is worse than a broken directive. `nginx.conf` is bind-mounted
(docker-compose.yml), so a running nginx keeps serving from the config it parsed
at boot: the break is invisible until something restarts, and then every
container that ever restarts is dead rather than degraded. Staging found it;
production was spared only because its checkout predates the commit, and the
local dev stack was serving happily from a config it could no longer reload.

`bin/check_nginx_config.sh` asks nginx itself and catches everything this
cannot; it needs Docker, which the test container does not have.
"""

from __future__ import annotations

import re

from urbanlens.core.tests.nginx_config import NGINX_DIR, directives_by_context, misplaced_directives
from urbanlens.core.tests.testcase import SimpleTestCase


class TheConfigLoadsTests(SimpleTestCase):
    """Directives sit in blocks nginx accepts them in."""

    def test_no_directive_sits_in_a_block_that_rejects_it(self) -> None:
        text = (NGINX_DIR / "nginx.conf").read_text(encoding="utf-8")

        wrong = misplaced_directives(text)

        self.assertEqual(wrong, [], f"nginx.conf: {wrong} - nginx would refuse to start")

    def test_every_zone_the_vhost_uses_is_declared(self) -> None:
        """`limit_conn foo` against an undeclared `foo` is also a refusal to
        start, and splitting declaration from use across two files makes it an
        easy one to land."""
        declared = set(re.findall(r"zone=(\w+)[:\s]", (NGINX_DIR / "nginx.conf").read_text(encoding="utf-8")))

        for config in sorted(NGINX_DIR.glob("*.conf")) + sorted(NGINX_DIR.glob("*.conf.template")):
            used = set(
                re.findall(r"^\s*limit_(?:conn|req)\s+(\w+)\s", config.read_text(encoding="utf-8"), re.MULTILINE)
            )

            self.assertEqual(used - declared, set(), f"{config.name} names zones nginx.conf never declares")


class TheGuardCanFailTests(SimpleTestCase):
    """A checker that called every config clean would pass the tests above
    without reading anything, so each is paired with the shape it exists to
    catch - the outage as it was actually written."""

    BROKEN = """
    events {
        worker_connections 1024;
        limit_conn_zone $binary_remote_addr zone=ws_conn:10m;
    }
    http {
        server { location /ws/ { limit_conn ws_conn 60; } }
    }
    """

    def test_the_checker_catches_the_outage(self) -> None:
        self.assertEqual(misplaced_directives(self.BROKEN), [("limit_conn_zone", ("events",), 4)])

    def test_the_fixed_placement_is_accepted(self) -> None:
        """Anti-vacuity in the other direction: a checker that rejected every
        config would also pass the test above."""
        fixed = self.BROKEN.replace("        limit_conn_zone $binary_remote_addr zone=ws_conn:10m;\n", "").replace(
            "    http {",
            "    http {\n        limit_conn_zone $binary_remote_addr zone=ws_conn:10m;",
        )

        self.assertEqual(misplaced_directives(fixed), [])

    def test_the_parser_reports_real_contexts(self) -> None:
        """Placement is the whole assertion, so the contexts have to be right -
        a parser that returned `()` for everything would call the break clean."""
        parsed = directives_by_context(self.BROKEN)

        self.assertIn(((), "events", 2), parsed)
        self.assertIn((("events",), "worker_connections", 3), parsed)
        self.assertIn((("http", "server", "location"), "limit_conn", 7), parsed)
