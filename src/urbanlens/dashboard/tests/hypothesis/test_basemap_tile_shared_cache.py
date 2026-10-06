"""Who may keep a proxied basemap tile: the viewer's browser, for as long as the vendor allows, and nobody else.

The endpoint is behind a login, so a shared cache holding a tile would hand it to anyone who asked, and its hit or
miss would say whether somebody here had looked at that coordinate. The browser is the cache that matters anyway:
it is what keeps a pan back over the same ground off the proxy. It only does that if the layers below the view do
not tie the response to one viewer - sessions, auth and the media cookie all touch it after the view returns, and a
``Vary: Cookie`` makes every cookie refresh a miss on bytes that did not change.
"""

from __future__ import annotations

from django.http import HttpResponse, HttpResponseRedirect
from django.test import SimpleTestCase

from urbanlens.dashboard.controllers.basemap_tiles import _browser_ttl, _keep_for
from urbanlens.dashboard.middleware import VIEWER_INDEPENDENT_ATTR, SecurityHeadersMiddleware, mark_viewer_independent
from urbanlens.dashboard.services.map.basemap_vendors import VENDOR_TILES


def _served(response: HttpResponse) -> HttpResponse:
    """Put a response through the middleware that runs last, as a real one would be."""
    return SecurityHeadersMiddleware(lambda _request: response)(None)


def _directives(response: HttpResponse) -> set[str]:
    return {part.strip().lower() for part in response.headers.get("Cache-Control", "").split(",") if part.strip()}


def _browser_misses(response: HttpResponse) -> list[str]:
    """Why a browser would not answer the next request for this URL from what it kept, if it would not."""
    reasons: list[str] = []
    directives = _directives(response)
    if not any(d.startswith("max-age=") and d != "max-age=0" for d in directives):
        reasons.append("no lifetime")
    if directives & {"no-store", "no-cache"}:
        reasons.append("Cache-Control forbids keeping it")
    vary = [part.strip().lower() for part in response.headers.get("Vary", "").split(",") if part.strip()]
    if "cookie" in vary:
        reasons.append("Vary names Cookie")
    if "*" in vary:
        reasons.append("Vary is *")
    return reasons


class OnlyTheViewersBrowserKeepsATileTests(SimpleTestCase):
    """The 200 and the definitive 404 - the two answers worth holding."""

    def test_no_shared_cache_may_keep_one(self) -> None:
        for response in (
            _keep_for(HttpResponse(b"tile", content_type="image/png")),
            _keep_for(HttpResponse(status=404)),
        ):
            directives = _directives(_served(response))
            self.assertIn("private", directives)
            self.assertNotIn("public", directives)
            self.assertFalse([d for d in directives if d.startswith("s-maxage")])

    def test_the_browser_keeps_it_through_everything_the_layers_below_add(self) -> None:
        """The session layer adds ``Vary: Cookie`` whenever it is read, and the media cookie middleware sets a
        cookie on any authenticated response due a refresh. Neither is visible from the view."""
        response = _keep_for(HttpResponse(b"tile", content_type="image/png"))
        response.headers["Vary"] = "Cookie"
        response.set_cookie("ul_media", "refreshed")

        served = _served(response)

        self.assertEqual(_browser_misses(served), [])
        self.assertFalse(served.cookies)

    def test_a_tile_is_kept_without_revalidating(self) -> None:
        served = _served(_keep_for(HttpResponse(b"tile", content_type="image/png")))

        self.assertIn("immutable", _directives(served))
        self.assertIn("max-age=604800", _directives(served))
        self.assertEqual(served.headers["X-Content-Type-Options"], "nosniff")

    def test_negotiated_encoding_still_varies(self) -> None:
        """Dropping Accept-Encoding along with Cookie would let a gzipped body reach a client that cannot read it."""
        response = _keep_for(HttpResponse(b"tile", content_type="image/png"))
        response.headers["Vary"] = "Accept-Encoding, Cookie"

        self.assertEqual(_served(response).headers["Vary"], "Accept-Encoding")

    def test_a_response_that_varies_on_nothing_says_so(self) -> None:
        response = _keep_for(HttpResponse(b"tile", content_type="image/png"))

        self.assertNotIn("Vary", _served(response).headers)


class TheVendorsLifetimeIsTheCeilingTests(SimpleTestCase):
    """Esri sends ``Cache-Control: max-age=86400``; the browser is told no longer, however long this deployment
    keeps the bytes itself."""

    def test_every_vendor_layer_is_cut_to_its_vendors_lifetime(self) -> None:
        for layer, vendor in VENDOR_TILES.items():
            with self.subTest(layer=layer):
                self.assertIsNotNone(vendor.browser_max_age)
                self.assertEqual(_browser_ttl(layer, 365 * 86400), vendor.browser_max_age)

    def test_a_shorter_life_of_our_own_is_kept(self) -> None:
        self.assertEqual(_browser_ttl("satellite", 3600), 3600)

    def test_a_layer_no_vendor_table_names_keeps_this_deployments_lifetime(self) -> None:
        self.assertEqual(_browser_ttl("some_redata_layer", 604800), 604800)

    def test_at_least_a_day_where_the_vendor_allows_a_day(self) -> None:
        self.assertGreaterEqual(_browser_ttl("satellite", 604800), 86400)


class OnlyMarkedResponsesAreStrippedTests(SimpleTestCase):
    """The mark is the whole safety boundary: stripping is destructive to anything else."""

    def test_a_signed_out_visitors_redirect_keeps_its_cookies(self) -> None:
        """The tile view answers an anonymous request with `handle_no_permission()`, which never reaches
        `_keep_for`. Stripping that one would drop the session cookie carrying the `next` round trip."""
        response = HttpResponseRedirect("/accounts/login/")
        response.set_cookie("sessionid", "abc")
        response.headers["Vary"] = "Cookie"

        served = _served(response)

        self.assertEqual(served.cookies["sessionid"].value, "abc")
        self.assertEqual(served.headers["Vary"], "Cookie")

    def test_a_503_is_left_unmarked_and_without_a_lifetime(self) -> None:
        """Slot exhaustion and an unreachable upstream both answer 503 without the stamp. Keeping one would turn a
        passing outage into a blank map region."""
        served = _served(HttpResponse(status=503, headers={"Retry-After": "1"}))

        self.assertFalse(getattr(served, VIEWER_INDEPENDENT_ATTR, False))
        self.assertIn("no lifetime", _browser_misses(served))


class TheStripRunsAfterTheLayersItUndoesTests(SimpleTestCase):
    """Position, not behaviour - but the mechanism is silently inert if it ever moves.

    `SecurityHeadersMiddleware` only sees headers added by middleware listed below it, because the response phase
    runs in reverse. Below `SessionMiddleware` it would strip a `Vary: Cookie` that the session layer then adds
    straight back, and nothing would fail except the browser's hit rate.
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


class MarkViewerIndependentTests(SimpleTestCase):
    def test_it_returns_the_same_response_so_it_can_wrap_a_return(self) -> None:
        response = HttpResponse(b"x")

        self.assertIs(mark_viewer_independent(response), response)
        self.assertTrue(getattr(response, VIEWER_INDEPENDENT_ATTR, False))
