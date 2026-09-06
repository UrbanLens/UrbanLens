"""`UL_SITE_URL`'s default names a port nothing serves.

`docker-compose.yml` defaults `UL_SITE_URL` to `http://localhost:21080` and
publishes the app on `UL_APP_PORT`, whose default is `21800` - the same four
digits, transposed. Any deployment that does not set `UL_SITE_URL` explicitly
therefore tells the app it lives somewhere it does not.

Three things break, and none of them look like a port typo:

- Every unsafe request from a browser is rejected on CSRF origin checking.
  `_derive_trusted_origins` mints origins from `ALLOWED_HOSTS`, which never
  carries a port (Django strips it before matching), so `UL_SITE_URL` is the
  only place the port can come from - and the header the browser actually
  sends is `Origin: http://localhost:<the real port>`.
- Every URL the app builds for itself points at the wrong port.
- `MEDIA_FRAME_ANCESTORS` gets that origin too, so the Vault lightbox's
  cross-origin iframe is refused by the media host.

Found 2026-09-06 when a browser POST to a new endpoint came back 403 with
"Origin checking failed" on the dev stack.
"""

from __future__ import annotations

import pathlib
import re

from urbanlens.core.tests.testcase import SimpleTestCase

#: The repo root - parents[5] from src/urbanlens/dashboard/tests/hypothesis/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"

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
        # A deployment where one of them disagrees builds different URLs
        # depending on which process wrote them, which is the shape P23 records
        # in production.
        self.assertEqual(len(_url_defaults("UL_SITE_URL")), 1, _url_defaults("UL_SITE_URL"))

    def test_the_media_host_fails_closed_rather_than_guessing_an_origin(self) -> None:
        # `MEDIA_FRAME_ANCESTORS: ${UL_SITE_URL:-'none'}` is the one place the
        # default is deliberately not a URL: a deployment nobody configured
        # should refuse framing rather than allow it from a guess.
        self.assertIn("'none'", _defaults("UL_SITE_URL"))
