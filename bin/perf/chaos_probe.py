#!/usr/bin/env python3
r"""Assert what should still work while one piece of the infrastructure is broken."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
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
    """A signed-in browser, roughly.

    Carries its own cookies rather than using `http.cookiejar`, which appends ``.local`` to a dotless host - so
    a cookie set by `localhost` is stored under `localhost.local` and never sent back."""

    base_url: str
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def csrf(self) -> str:
        """The current CSRF token."""
        return self.cookies.get("csrftoken", "")

    def absorb(self, response: object) -> None:
        """Take any Set-Cookie headers from *response*."""
        headers = getattr(response, "headers", None)
        if headers is None:
            return
        for raw in headers.get_all("Set-Cookie") or []:
            pair = raw.split(";", 1)[0].strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.cookies[name.strip()] = value.strip()

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Request headers, including the cookies this session holds."""
        head = {"Referer": self.base_url}
        if self.cookies:
            head["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        head.update(extra or {})
        return head

    def get(self, path: str) -> tuple[int, str]:
        """GET *path*, returning the status and body (empty on a transport error)."""
        request = urllib.request.Request(f"{self.base_url}{path}", headers=self.headers())  # noqa: S310 - scheme validated in main()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme validated in main()
                self.absorb(response)
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


def _uncached_pins_answer(session: Session) -> tuple[bool, str]:
    """The pins endpoint on a path the cache cannot serve.

    `map_pins_json` only caches the profile's whole unbounded root-pin set, so a `bbox` request is explicitly
    `cacheable=False` and must reach Postgres."""
    status, body = session.get("/dashboard/map/pins/?bbox=-31,-141,-28,-138&limit=5")
    if status == 0:
        return False, f"no answer ({body[:60]})"
    if status in {503, 429}:
        # Refusing under pressure is the wanted behaviour, not a failure.
        return True, f"refused cleanly with HTTP {status}"
    if status != 200:
        return False, f"HTTP {status}"
    try:
        payload = json.loads(body)
    except ValueError:
        return False, "not JSON"
    return "pins" in payload, f"cache={payload.get('cache', '?')}"


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
    _, body = session.get("/dashboard/map/pins/?bbox=-31,-141,-28,-138&limit=5")
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
        # Deliberately the bbox path: the cached one answers without a
        # connection, so it reports a pass for something it never exercised.
        Expectation("uncached pins answer or refuse cleanly", _uncached_pins_answer, known="P104"),
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


def establish(base_url: str, manifest: Path, role: str, session_file: Path | None) -> Session:
    """A signed-in session, from a saved one when there is one.

    The saved path matters: every scenario here asks what an *already* signed-in user still sees while something
    is broken.

    Args:
        base_url: Origin under test.
        manifest: Provisioning manifest.
        role: Which account.
        session_file: Where cookies are kept.

    Returns:
        A session with cookies loaded."""
    if session_file is not None and session_file.exists():
        return Session(base_url, json.loads(session_file.read_text(encoding="utf-8")))

    session = sign_in(base_url, manifest, role)
    if session_file is not None:
        session_file.write_text(json.dumps(session.cookies), encoding="utf-8")
        session_file.chmod(0o600)
    return session


def sign_in(base_url: str, manifest: Path, role: str) -> Session:
    """Sign in as *role* from the provisioning manifest.

    Redirects are deliberately not followed: a successful sign-in is a 302, and following it without carrying
    the new session cookie lands back on the login page and reads as a failure that did not happen.

    Raises:
        SystemExit: The manifest has no such role, or sign-in did not happen."""
    accounts = json.loads(manifest.read_text(encoding="utf-8")).get("accounts", [])
    account = next((entry for entry in accounts if entry["role"] == role), None)
    if account is None:
        raise SystemExit(f"The manifest has no '{role}' account; it has: {', '.join(a['role'] for a in accounts) or 'none'}.")

    session = Session(base_url)
    login_url = f"{base_url}/accounts/login/"
    status, _ = session.get("/accounts/login/")
    if status != 200:
        raise SystemExit(f"GET {login_url} answered {status}; the target is not serving the sign-in page.")
    if not session.csrf:
        raise SystemExit(f"No csrftoken from {login_url}; something in front of the app is stripping Set-Cookie.")

    body = urllib.parse.urlencode(
        {"csrfmiddlewaretoken": session.csrf, "username": account["username"], "password": account["password"]},
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - scheme validated in main()
        login_url,
        data=body,
        headers=session.headers({"Referer": login_url, "Origin": base_url}),
    )
    try:
        with _NO_REDIRECTS.open(request, timeout=TIMEOUT_SECONDS) as response:
            session.absorb(response)
            raise SystemExit(f"Sign-in as {account['username']} answered {response.status} rather than redirecting; the form was re-rendered, so the credentials were refused.")
    except urllib.error.HTTPError as error:
        session.absorb(error)
        if error.code != 302:
            # A finding rather than a crash: signing in is a thing that can break while the infrastructure is
            # broken.
            raise SystemExit(f"Sign-in as {account['username']} was refused with HTTP {error.code}. If a cache outage is injected, that is P105.") from error

    if "sessionid" not in session.cookies:
        raise SystemExit(f"Sign-in as {account['username']} redirected but left no session cookie.")
    return session


class _StopRedirects(urllib.request.HTTPRedirectHandler):
    """Surface a 302 as an HTTPError instead of following it."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Never follow."""
        return


_NO_REDIRECTS = urllib.request.build_opener(_StopRedirects)


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
    parser.add_argument(
        "--session-file",
        type=Path,
        default=None,
        help=(
            "Where to keep the signed-in cookies. Written on first use and reused after, so the "
            "session is established BEFORE the failure is injected - which is the premise every "
            "scenario is written against. Signing in during a cache outage tests the sign-in path "
            "instead, which is P105 and a different question."
        ),
    )
    args = parser.parse_args(argv)

    base_url = args.url.rstrip("/")
    scheme = urllib.parse.urlparse(base_url).scheme
    if scheme not in _PERMITTED_SCHEMES:
        raise SystemExit(f"--url must be http or https, not {scheme!r}.")
    session = establish(base_url, args.manifest, args.role, args.session_file)

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
