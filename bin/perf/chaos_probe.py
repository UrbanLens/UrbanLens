#!/usr/bin/env python3
r"""Assert what should still work while one piece of the infrastructure is broken.

`infrastructure/bin/chaos.py` breaks a dev environment and guarantees the
restore; it deliberately asserts nothing, because whether the application then
degrades or falls over is a question about the application. This is that half.

Run it as the command of an injection::

    chaos.py inject perf cache-outage --for 180s -- \\
        bin/perf/chaos_probe.py --url https://perf.dev.urbanlens.org \\
            --manifest /tmp/perf.json --scenario cache-outage

Each scenario carries the expectations from the availability programme (PL7
§4.5). Some of them are **expected to fail today** and are marked `known`: those
are recorded problems, and the point of running this is to watch them turn from
predictions into measurements, and later to notice the day they stop failing.
An `unexpected` failure is the interesting one - something degraded in a way
nobody had written down.

Exit status is the number of unexpected failures, so a green run is 0 and
chaos.py propagates it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING
import urllib.error
import urllib.parse
import urllib.request

if TYPE_CHECKING:
    from collections.abc import Callable

#: How long any single probe may take before it counts as "did not answer".
#: Generous: the question is whether the site answers at all, not how fast.
TIMEOUT_SECONDS = 20.0

#: Schemes `--url` may use. Checked once at entry so the opener calls below are
#: reaching a network URL and not `file:` or something stranger.
_PERMITTED_SCHEMES = frozenset({"http", "https"})


@dataclass
class Expectation:
    """One thing that should still be true while the failure is injected."""

    name: str
    check: Callable[[Session], tuple[bool, str]]
    #: A problem id when this is known to fail today, else empty.
    known: str = ""


@dataclass
class Session:
    """A signed-in browser, roughly."""

    base_url: str
    opener: urllib.request.OpenerDirector
    csrf: str = ""

    def get(self, path: str) -> tuple[int, str]:
        """GET *path*, returning the status and body (empty on a transport error)."""
        request = urllib.request.Request(f"{self.base_url}{path}", headers={"Referer": self.base_url})  # noqa: S310 - scheme validated in main()
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                return response.status, response.read(200_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read(20_000).decode("utf-8", "replace")
        except Exception as error:  # a transport failure is a result here, not a crash
            return 0, str(error)


def _health_answers(session: Session) -> tuple[bool, str]:
    status, body = session.get("/health/ready")
    if status == 0:
        return False, f"no answer at all ({body[:80]})"
    # 200 preferred, but the property under test is that a probe gets a verdict
    # rather than hanging - a readiness check that times out removes the
    # instances still serving, which is worse than one that says "degraded".
    return status in {200, 503}, f"HTTP {status}"


def _authenticated_page_renders(session: Session) -> tuple[bool, str]:
    status, body = session.get("/dashboard/map/")
    if status != 200:
        return False, f"HTTP {status}"
    if "/accounts/login" in body[:2000]:
        return False, "redirected to the sign-in page"
    return True, "rendered"


def _pins_endpoint_answers(session: Session) -> tuple[bool, str]:
    status, body = session.get("/dashboard/map/pins/?limit=5")
    if status != 200:
        return False, f"HTTP {status}"
    try:
        payload = json.loads(body)
    except ValueError:
        return False, "not JSON"
    return "pins" in payload, f"cache={payload.get('cache', '?')}"


def _no_traceback_leaked(session: Session) -> tuple[bool, str]:
    """A failure should be a status code, not a stack trace on the page."""
    _, body = session.get("/dashboard/map/pins/?limit=5")
    leaked = "Traceback (most recent call last)" in body
    return not leaked, "traceback in the response body" if leaked else "clean"


SCENARIOS: dict[str, list[Expectation]] = {
    "cache-outage": [
        Expectation("readiness answers", _health_answers),
        Expectation("signed-in page still renders", _authenticated_page_renders),
        Expectation("map pins still answer", _pins_endpoint_answers),
        Expectation("no traceback leaked", _no_traceback_leaked),
    ],
    "connection-exhaustion": [
        Expectation("readiness answers", _health_answers),
        # P104's shape: the endpoint should refuse quickly rather than raise.
        Expectation("map pins answer or refuse cleanly", _pins_endpoint_answers, known="P104"),
        Expectation("no traceback leaked", _no_traceback_leaked, known="P104"),
    ],
    "cpu-saturation": [
        Expectation("readiness answers", _health_answers),
        Expectation("signed-in page still renders", _authenticated_page_renders),
        Expectation("map pins still answer", _pins_endpoint_answers),
    ],
    "worker-outage": [
        # Nothing in the request path should need a Celery worker.
        Expectation("readiness answers", _health_answers),
        Expectation("signed-in page still renders", _authenticated_page_renders),
        Expectation("map pins still answer", _pins_endpoint_answers),
    ],
}


def sign_in(base_url: str, manifest: Path, role: str) -> Session:
    """Sign in as *role* from the provisioning manifest.

    Raises:
        SystemExit: The manifest has no such role, or sign-in did not happen.
    """
    accounts = json.loads(manifest.read_text(encoding="utf-8")).get("accounts", [])
    account = next((entry for entry in accounts if entry["role"] == role), None)
    if account is None:
        raise SystemExit(f"The manifest has no '{role}' account; it has: {', '.join(a['role'] for a in accounts) or 'none'}.")

    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    session = Session(base_url, opener)

    login_url = f"{base_url}/accounts/login/"
    with opener.open(login_url, timeout=TIMEOUT_SECONDS) as response:
        response.read()
    token = next((cookie.value for cookie in jar if cookie.name == "csrftoken"), "")
    if not token:
        raise SystemExit(f"No csrftoken from {login_url}; something in front of the app is stripping Set-Cookie.")

    body = urllib.parse.urlencode(
        {"csrfmiddlewaretoken": token, "username": account["username"], "password": account["password"]},
    ).encode()
    request = urllib.request.Request(login_url, data=body, headers={"Referer": login_url, "Origin": base_url})  # noqa: S310 - scheme validated in main()
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        landed = response.geturl()
    if "/accounts/login" in landed:
        raise SystemExit(f"Sign-in as {account['username']} did not happen; still at {landed}.")

    session.csrf = next((cookie.value for cookie in jar if cookie.name == "csrftoken"), "")
    return session


def main(argv: list[str] | None = None) -> int:
    """Probe one scenario's expectations and report.

    Returns:
        The number of *unexpected* failures.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Base URL of the environment under test.")
    parser.add_argument("--manifest", required=True, type=Path, help="Manifest from provision_integration_env.")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS), help="Which injection is running.")
    parser.add_argument("--role", default="secondary", help="Account to probe as (default: secondary).")
    args = parser.parse_args(argv)

    base_url = args.url.rstrip("/")
    scheme = urllib.parse.urlparse(base_url).scheme
    if scheme not in _PERMITTED_SCHEMES:
        raise SystemExit(f"--url must be http or https, not {scheme!r}.")
    session = sign_in(base_url, args.manifest, args.role)

    print(f"\n{args.scenario}: what should still work")
    unexpected = 0
    for expectation in SCENARIOS[args.scenario]:
        ok, detail = expectation.check(session)
        if ok:
            verdict = "ok" if not expectation.known else f"ok (was expected to fail: {expectation.known})"
        elif expectation.known:
            verdict = f"FAILED as expected ({expectation.known})"
        else:
            verdict = "FAILED - unexpected"
            unexpected += 1
        print(f"  {expectation.name:<34} {verdict:<34} {detail}")

    print(f"\n{unexpected} unexpected failure(s).\n")
    return unexpected


if __name__ == "__main__":
    sys.exit(main())
