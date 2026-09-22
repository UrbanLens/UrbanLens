"""Whether a CDN will actually store a proxied basemap tile.

The proxy's throughput is bounded by ``basemap_tile_upstream_concurrency`` slots per process, so a
viewport of ~30 tiles is served a few at a time no matter how much hardware is behind it. The only
way out is for the tiles not to reach the origin at all, which needs an edge cache to hold them -
and an edge cache refuses for reasons a view cannot see, because sessions, auth and the media
cookie all touch the response after the view has returned.

So these tests assert the property that decides it - "a shared cache may store this" - rather than
the individual headers, which are only the current spelling of it.
"""

from __future__ import annotations

from django.http import HttpResponse, HttpResponseRedirect
from django.test import SimpleTestCase

from urbanlens.dashboard.controllers.basemap_tiles import _keep_for_a_week
from urbanlens.dashboard.middleware import SHARED_CACHE_ATTR, SecurityHeadersMiddleware, mark_shared_cacheable


def _served(response: HttpResponse) -> HttpResponse:
    """Put a response through the middleware that runs last, as a real one would be."""
    return SecurityHeadersMiddleware(lambda _request: response)(None)


def _refusals(response: HttpResponse) -> list[str]:
    """Why a shared cache would decline to store this response, if it would.

    Each entry is one of Cloudflare's documented reasons for `BYPASS`/`DYNAMIC`. An empty list
    means the response is storable.
    """
    reasons: list[str] = []
    directives = {part.strip().lower() for part in response.headers.get("Cache-Control", "").split(",")}
    if "public" not in directives:
        reasons.append("Cache-Control does not say public")
    if any(d in directives for d in ("private", "no-store", "no-cache", "max-age=0")):
        reasons.append("Cache-Control forbids a shared cache")
    if response.cookies:
        reasons.append("Set-Cookie is present")
    vary = [part.strip().lower() for part in response.headers.get("Vary", "").split(",") if part.strip()]
    if "cookie" in vary:
        reasons.append("Vary names Cookie")
    if "*" in vary:
        reasons.append("Vary is *")
    return reasons


class TileResponsesAreStorableByASharedCacheTests(SimpleTestCase):
    """The 200 and the definitive 404 - the two answers worth holding."""

    def test_a_tile_survives_everything_the_layers_below_add_to_it(self) -> None:
        """The regression this exists for: a view saying `public` is not enough on its own.

        The session layer adds ``Vary: Cookie`` whenever it is read, and the media cookie
        middleware calls ``set_media_cookie`` on any authenticated response due a refresh. Either
        one alone is a `BYPASS`, and neither is visible from the view.
        """
        response = _keep_for_a_week(HttpResponse(b"tile", content_type="image/png"))
        response.headers["Vary"] = "Cookie"
        response.set_cookie("ul_media", "refreshed")

        self.assertEqual(_refusals(_served(response)), [])

    def test_a_definitive_404_is_storable_too(self) -> None:
        """A blank area is re-asked on every pan over the same ground otherwise."""
        response = _keep_for_a_week(HttpResponse(status=404))
        response.headers["Vary"] = "Cookie"

        self.assertEqual(_refusals(_served(response)), [])

    def test_the_browser_is_still_told_to_keep_it(self) -> None:
        """Edge caching is the new half; the browser cache was already load-bearing."""
        served = _served(_keep_for_a_week(HttpResponse(b"tile", content_type="image/png")))

        self.assertIn("immutable", served.headers["Cache-Control"])
        self.assertIn("max-age=604800", served.headers["Cache-Control"])
        self.assertEqual(served.headers["X-Content-Type-Options"], "nosniff")

    def test_negotiated_encoding_still_varies(self) -> None:
        """Accept-Encoding is the one Vary a CDN keys on rather than ignores, so dropping it
        wholesale would let a gzipped body reach a client that cannot read it."""
        response = _keep_for_a_week(HttpResponse(b"tile", content_type="image/png"))
        response.headers["Vary"] = "Accept-Encoding, Cookie"

        self.assertEqual(_served(response).headers["Vary"], "Accept-Encoding")

    def test_a_response_that_varies_on_nothing_says_so(self) -> None:
        response = _keep_for_a_week(HttpResponse(b"tile", content_type="image/png"))

        self.assertNotIn("Vary", _served(response).headers)


class OnlyMarkedResponsesAreStrippedTests(SimpleTestCase):
    """The mark is the whole safety boundary: stripping is destructive to anything else."""

    def test_a_signed_out_visitors_redirect_keeps_its_cookies(self) -> None:
        """The tile view answers an anonymous request with `handle_no_permission()`, which never
        reaches `_keep_for_a_week`. Stripping that one would drop the session cookie carrying the
        `next` round trip, and cache a login redirect under a tile's URL."""
        response = HttpResponseRedirect("/accounts/login/")
        response.set_cookie("sessionid", "abc")
        response.headers["Vary"] = "Cookie"

        served = _served(response)

        self.assertEqual(served.cookies["sessionid"].value, "abc")
        self.assertEqual(served.headers["Vary"], "Cookie")
        self.assertNotEqual(_refusals(served), [])

    def test_a_503_is_left_uncacheable(self) -> None:
        """Slot exhaustion and an unreachable upstream both answer 503 without the stamp. Holding
        one would turn a passing outage into a permanently blank map region."""
        response = HttpResponse(status=503, headers={"Retry-After": "1"})

        served = _served(response)

        self.assertFalse(getattr(served, SHARED_CACHE_ATTR, False))
        self.assertNotEqual(_refusals(served), [])

    def test_the_catalogue_is_not_marked_by_the_tile_stamp(self) -> None:
        """`/sources/` answers a different layer list to a signed-in viewer than to a signed-out
        one, so it is the one thing on this prefix that must never be shared."""
        response = HttpResponse(b'{"layers": []}', content_type="application/json")

        self.assertNotEqual(_refusals(_served(response)), [])


class TheStripRunsAfterTheLayersItUndoesTests(SimpleTestCase):
    """Position, not behaviour - but the mechanism is silently inert if it ever moves.

    `SecurityHeadersMiddleware` only sees headers added by middleware listed below it, because the
    response phase runs in reverse. Below `SessionMiddleware` it would strip a `Vary: Cookie` that
    the session layer then adds straight back, and nothing would fail except the cache hit rate.
    """

    def test_the_stripper_is_listed_above_every_layer_that_marks_a_response_per_viewer(self) -> None:
        from django.conf import settings

        order = list(settings.MIDDLEWARE)
        stripper = order.index("urbanlens.dashboard.middleware.SecurityHeadersMiddleware")

        for name in (
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "urbanlens.dashboard.middleware.MediaOriginCookieMiddleware",
        ):
            self.assertLess(stripper, order.index(name), f"{name} runs after the strip and would undo it")


class MarkSharedCacheableTests(SimpleTestCase):
    def test_it_returns_the_same_response_so_it_can_wrap_a_return(self) -> None:
        response = HttpResponse(b"x")

        self.assertIs(mark_shared_cacheable(response), response)
        self.assertTrue(getattr(response, SHARED_CACHE_ATTR, False))
