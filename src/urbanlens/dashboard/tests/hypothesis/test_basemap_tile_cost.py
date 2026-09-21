"""What one basemap tile costs this deployment, and what a viewport of them costs together.

The tile proxy is the highest-frequency authenticated endpoint on the site: a viewport is ~30 tiles
and the browser asks for all of them at once, so whatever one tile costs is paid ~30 times per map
opened, per user. Gunicorn serves the whole site on ``workers x threads`` request threads
(``-w $WEB_CONCURRENCY --threads 4``), which makes per-tile cost the thing that decides how many
people can open a map at the same time, far more than upstream speed does.

REData is stubbed everywhere here, deliberately twice over: these numbers are about this
deployment's own cost, and a performance test must not put load on someone else's service to
measure it.

The assertions are budgets on things that are deterministic - queries, headers, bytes, upstream
calls. Wall-clock is measured and reported rather than asserted: this host is shared, and a timing
assertion would fail for reasons that have nothing to do with the code. Run with ``-s`` to read the
reported figures.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import basemap_tiles

if TYPE_CHECKING:
    from django.http import HttpResponse

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

#: A document response, as the control for anything asserting a tile does not carry a document header.
_DOCUMENT_URL = "/dashboard/map/"

#: Tiles in one cold viewport - the unit every figure here is really about.
VIEWPORT_TILES = 30

#: A 256px basemap tile, near the top of the usual range.
TILE_BYTES = 12_000

#: Queries one cached tile may cost on a session that has already drawn one. Zero: the session's
#: tile access is remembered in the same cache the bytes come from
#: (``services/map/tile_authorisation.py``), and nothing else in the chain loads the viewer's row
#: unless the request writes. Raising this is a real regression - it is paid ~30 times per map.
MAX_QUERIES_PER_CACHED_TILE = 0

#: Queries a whole warm viewport may cost. A session establishes its tile access once, so this is a
#: per-session cost rather than a per-tile one - which is the entire point of the arrangement.
MAX_QUERIES_PER_WARM_VIEWPORT = 1

#: Bytes of response headers one tile may carry. A tile body is ~12kB, so a page's worth of
#: document-level headers on it is real bandwidth spent ~30 times per map. Security headers that do
#: something on an image (``nosniff``, CORP) stay; a Content-Security-Policy on a PNG does not.
MAX_HEADER_BYTES_PER_TILE = 600


def _tile_store():
    """The store the proxy reads tiles out of, which is not the default cache.

    Returns:
        The proxied-bytes cache.
    """
    return caches[settings.PROXIED_BYTES_CACHE]


def header_bytes(response: HttpResponse) -> int:
    """Roughly what this response's headers cost on the wire.

    Args:
        response: The response to measure.

    Returns:
        Bytes, counting ``name: value\\r\\n`` per header.
    """
    return sum(len(name) + len(str(value)) + 4 for name, value in response.headers.items())


def cache_directives(response: HttpResponse) -> str:
    """The response's ``Cache-Control`` value, or ``""`` when it has none."""
    return response.headers.get("Cache-Control", "")


class BasemapTileCostTests(TestCase):
    """The per-tile budget, measured on a warm cache - the path that carries every map after the first."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        basemap_tiles.UpstreamSlots.reset()
        self.addCleanup(basemap_tiles.UpstreamSlots.reset)
        # One tile before anything is measured. The session and its user are read from the database
        # once per client, and the session's tile access is established on its first tile - both are
        # per-session costs, and counting either in a per-tile budget would say a viewport costs
        # thirty of them when it costs one. `test_the_first_tile_of_a_session_pays_once` is where
        # that first tile is measured instead.
        self._warm_client()

    def _url(self, x: int = 1204, y: int = 1539, layer: str = "street", z: int = 12) -> str:
        return reverse("map.basemap_tiles", kwargs={"layer": layer, "z": z, "x": x, "y": y})

    def _warm_client(self) -> None:
        _tile_store().set("ul_basemap_tile_street_12_9999_1539", (b"x" * TILE_BYTES, "image/png"), 60)
        with mock.patch(_CONFIGURED, return_value=True):
            self.client.get(self._url(x=9999))

    def _serve_cached(self, x: int = 1204) -> HttpResponse:
        """Answer one tile from the cache, the way the second viewer of an area is answered."""
        _tile_store().set(f"ul_basemap_tile_street_12_{x}_1539", (b"x" * TILE_BYTES, "image/png"), 60)
        with mock.patch(_CONFIGURED, return_value=True):
            return self.client.get(self._url(x=x))

    def test_a_cached_tile_stays_inside_its_query_budget(self) -> None:
        """Every query here is paid ~30 times per map opened, by every viewer."""
        with mock.patch(_CONFIGURED, return_value=True):
            _tile_store().set("ul_basemap_tile_street_12_1204_1539", (b"x" * TILE_BYTES, "image/png"), 60)
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        print(f"\n  cached tile: {len(queries)} queries")
        for query in queries.captured_queries:
            print(f"    {query['sql'][:100]}")
        self.assertLessEqual(len(queries), MAX_QUERIES_PER_CACHED_TILE)

    def test_the_first_tile_of_a_session_pays_once(self) -> None:
        """Where the cost the rest of the tiles avoid actually goes, so it is bounded rather than moved.

        A budget of zero per tile measured only on a warmed session would say nothing about a
        session that opens a map for the first time - which every session does.
        """
        cache.clear()
        fresh = self.client_class()
        fresh.force_login(self.user)
        _tile_store().set("ul_basemap_tile_street_12_1204_1539", (b"x" * TILE_BYTES, "image/png"), 60)

        with mock.patch(_CONFIGURED, return_value=True), CaptureQueriesContext(connection) as first:
            response = fresh.get(self._url())
        with mock.patch(_CONFIGURED, return_value=True), CaptureQueriesContext(connection) as second:
            fresh.get(self._url())

        self.assertEqual(response.status_code, 200)
        print(f"\n  first tile of a session: {len(first)} queries; the next: {len(second)}")
        self.assertLessEqual(len(first), MAX_QUERIES_PER_WARM_VIEWPORT)
        self.assertLessEqual(len(second), MAX_QUERIES_PER_CACHED_TILE)

    def test_the_tile_view_itself_asks_the_database_for_nothing(self) -> None:
        """Separates this view's own cost from the chain's, so a regression lands on whoever caused it.

        The same request served from cache and refused outright touch entirely different code inside
        the view, so a query either of them makes that the other does not is the view's own."""
        _tile_store().set("ul_basemap_tile_street_12_1204_1539", (b"x" * TILE_BYTES, "image/png"), 60)
        with mock.patch(_CONFIGURED, return_value=True):
            with CaptureQueriesContext(connection) as served:
                self.client.get(self._url())
            with (
                mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNG", "image/png")),
                CaptureQueriesContext(connection) as fetched,
            ):
                self.client.get(self._url(x=1205))

        self.assertEqual(len(served.captured_queries), len(fetched.captured_queries))

    def test_a_cached_tile_tells_the_browser_it_may_keep_it(self) -> None:
        """Without this the browser re-asks for every tile on every pan back over the same ground.

        A tile is immutable for a given layer and coordinate - that is why the server keeps it for a
        week - so the answer it already has is the answer it would get."""
        response = self._serve_cached()

        directives = cache_directives(response)
        print(f"\n  cached tile Cache-Control: {directives!r}")
        self.assertIn("max-age=", directives)
        self.assertIn("private", directives, "a tile is served behind a login; a shared cache must not keep it")
        self.assertIn("immutable", directives, "re-validating a tile that cannot change is a round trip for nothing")
        max_age = int(directives.split("max-age=")[1].split(",")[0])
        self.assertGreaterEqual(max_age, 86400)

    def test_a_tile_carries_no_more_headers_than_it_needs(self) -> None:
        """Header bytes are paid per tile, so a page's worth of them is ~30x per map opened."""
        response = self._serve_cached()

        measured = header_bytes(response)
        print(f"\n  cached tile: {measured} header bytes on a {TILE_BYTES} byte body")
        for name, value in sorted(response.headers.items()):
            print(f"    {len(name) + len(str(value)) + 4:>5}  {name}")
        self.assertLessEqual(measured, MAX_HEADER_BYTES_PER_TILE)

    def test_a_tile_keeps_the_security_headers_that_do_something_to_an_image(self) -> None:
        """The header budget above is met by dropping what a PNG cannot use, never a real protection.

        ``nosniff`` is the one that matters most here: it is exactly what stops a proxied image
        being re-interpreted as something executable."""
        response = self._serve_cached()

        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("Cross-Origin-Resource-Policy", response.headers)
        self.assertNotIn("Content-Security-Policy", response.headers)
        self.assertNotIn("Content-Security-Policy-Report-Only", response.headers)

    def test_the_first_viewer_of_an_area_may_keep_the_tile_too(self) -> None:
        """The freshly-fetched tile is a different return path from the cached one.

        It is also the expensive one - it cost an upstream fetch - so it is the last tile that
        should have to be asked for twice."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNG", "image/png")),
        ):
            response = self.client.get(self._url(x=1211))

        self.assertEqual(response.status_code, 200)
        self.assertIn("max-age=", cache_directives(response))
        self.assertIn("private", cache_directives(response))

    def test_a_miss_the_server_already_remembers_is_not_re_asked_either(self) -> None:
        """The cached-sentinel path, which is how a hole in a layer is answered after the first ask."""
        _tile_store().set("ul_basemap_tile_street_12_1212_1539", "__ul_no_tile__", 60)
        with mock.patch(_CONFIGURED, return_value=True):
            response = self.client.get(self._url(x=1212))

        self.assertEqual(response.status_code, 404)
        self.assertIn("max-age=", cache_directives(response))

    def test_no_content_security_policy_is_sent_in_either_mode(self) -> None:
        """A tile is exempted from both spellings, because a deployment sends one or the other.

        The site emits the report-only header until ``UL_CSP_ENFORCE`` flips it, so an exemption
        written for only the header this deployment happens to send today comes back the day that
        setting changes. The map page under the same settings is the control: it proves the policy
        really is configured, so the tile's silence is the exemption and not an empty setting."""
        modes = {
            "CONTENT_SECURITY_POLICY": "Content-Security-Policy",
            "CONTENT_SECURITY_POLICY_REPORT_ONLY": "Content-Security-Policy-Report-Only",
        }
        for setting, header in modes.items():
            with (
                self.subTest(mode=setting),
                override_settings(**{setting: {"DIRECTIVES": {"default-src": ["'self'"]}}}),
            ):
                page = self.client.get(_DOCUMENT_URL)
                self.assertIn(header, page.headers, f"{setting} is not reaching responses, so this test proves nothing")

                self.assertNotIn(header, self._serve_cached(x=1213).headers)

    def test_a_definitive_miss_is_not_re_asked_on_every_pan(self) -> None:
        """The server already remembers this answer for a week; the browser should not have to ask."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(404, b"", "")),
        ):
            response = self.client.get(self._url(x=1207))

        self.assertEqual(response.status_code, 404)
        self.assertIn("max-age=", cache_directives(response))

    def test_a_deployment_with_no_redata_is_not_remembered_as_having_no_tiles(self) -> None:
        """This 404 says "not configured", which is a deployment's state and not a fact about the
        tile - cached in the browser, configuring REData would leave every map blank until it aged
        out."""
        with mock.patch(_CONFIGURED, return_value=False):
            response = self.client.get(self._url(x=1208))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("max-age=", cache_directives(response))

    def test_a_refusal_is_never_remembered_by_the_browser(self) -> None:
        """A 503 is "this process is busy", the most temporary answer the proxy gives."""
        with mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", 1):
            basemap_tiles.UpstreamSlots.reset()
            self.assertTrue(basemap_tiles.UpstreamSlots.semaphore().acquire(blocking=False))
            try:
                with (
                    mock.patch(_CONFIGURED, return_value=True),
                    mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNG", "image/png")),
                ):
                    response = self.client.get(self._url(x=1209))
            finally:
                basemap_tiles.UpstreamSlots.semaphore().release()

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("max-age=", cache_directives(response))

    def test_a_refused_tile_costs_no_more_than_a_served_one(self) -> None:
        """Refusing is what the proxy does most of under load, so it is the path that has to be cheap."""
        _tile_store().set("ul_basemap_tile_street_12_1204_1539", (b"x" * TILE_BYTES, "image/png"), 60)
        with mock.patch(_CONFIGURED, return_value=True):
            with CaptureQueriesContext(connection) as served:
                self.client.get(self._url())

            with mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", 1):
                basemap_tiles.UpstreamSlots.reset()
                self.assertTrue(basemap_tiles.UpstreamSlots.semaphore().acquire(blocking=False))
                try:
                    with CaptureQueriesContext(connection) as refused:
                        self.assertEqual(self.client.get(self._url(x=1210)).status_code, 503)
                finally:
                    basemap_tiles.UpstreamSlots.semaphore().release()

        self.assertLessEqual(len(refused.captured_queries), len(served.captured_queries))


class BasemapViewportCostTests(TestCase):
    """What a whole viewport costs - the unit a user actually experiences, and the site pays for."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        basemap_tiles.UpstreamSlots.reset()
        self.addCleanup(basemap_tiles.UpstreamSlots.reset)
        with mock.patch(_CONFIGURED, return_value=False):
            self.client.get(reverse("map.basemap_tiles", kwargs={"layer": "street", "z": 12, "x": 1, "y": 1}))

    def _viewport_urls(self) -> list[str]:
        return [
            reverse("map.basemap_tiles", kwargs={"layer": "street", "z": 12, "x": 1200 + index, "y": 1539})
            for index in range(VIEWPORT_TILES)
        ]

    def _warm_the_cache(self, urls: list[str]) -> None:
        for index in range(len(urls)):
            _tile_store().set(f"ul_basemap_tile_street_12_{1200 + index}_1539", (b"x" * TILE_BYTES, "image/png"), 60)

    def test_a_warm_viewport_stays_inside_the_budget_every_tile_agreed_to(self) -> None:
        """One viewport, served entirely from cache: the ordinary case once an area has any visitors.

        Measured here rather than multiplied out from the single-tile test, because per-request
        state (a cached session, a reused connection) makes the thirtieth tile cheaper than the
        first, and the total is what the request threads actually spend."""
        urls = self._viewport_urls()
        self._warm_the_cache(urls)

        with mock.patch(_CONFIGURED, return_value=True):
            started = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                statuses = [self.client.get(url).status_code for url in urls]
            elapsed = time.perf_counter() - started

        self.assertEqual(set(statuses), {200})
        per_tile_ms = elapsed / len(urls) * 1000
        print(
            f"\n  warm viewport ({len(urls)} tiles): {len(queries)} queries, "
            f"{elapsed * 1000:.0f} ms total, {per_tile_ms:.2f} ms/tile"
        )
        print(
            f"    one thread serves ~{1000 / per_tile_ms:.0f} tiles/s; 12 request threads ~{12_000 / per_tile_ms:.0f} tiles/s"
        )
        self.assertLessEqual(len(queries), MAX_QUERIES_PER_WARM_VIEWPORT)

    def test_a_cold_viewport_never_asks_the_upstream_for_more_than_its_slots(self) -> None:
        """The bound is what keeps one cold map from occupying every request thread in the process.

        Held directly rather than from threads: this test's own transaction owns its connection, so
        a request served on another thread cannot see the user it signed in as."""
        urls = self._viewport_urls()
        with mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", 2):
            basemap_tiles.UpstreamSlots.reset()
            semaphore = basemap_tiles.UpstreamSlots.semaphore()
            self.assertTrue(semaphore.acquire(blocking=False))
            self.assertTrue(semaphore.acquire(blocking=False))
            try:
                with (
                    mock.patch(_CONFIGURED, return_value=True),
                    mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNG", "image/png")) as download,
                ):
                    statuses = [self.client.get(url).status_code for url in urls]
            finally:
                semaphore.release()
                semaphore.release()

        self.assertEqual(set(statuses), {503})
        self.assertEqual(download.call_count, 0, "every slot was taken, so nothing may have reached the upstream")

    def test_refusing_a_viewport_costs_no_more_queries_than_serving_one(self) -> None:
        """Under load the proxy refuses far more tiles than it serves, so a refusal that cost more
        than a hit would make load worse in proportion to how loaded it already is.

        Asserted on queries and reported on time: the two timings below are dominated by whatever
        cache backend the run has (locmem under the test settings, Dragonfly in a deployment) and
        say more about the harness than about this code."""
        urls = self._viewport_urls()
        self._warm_the_cache(urls)
        with mock.patch(_CONFIGURED, return_value=True):
            started = time.perf_counter()
            with CaptureQueriesContext(connection) as serving:
                for url in urls:
                    self.client.get(url)
            served = time.perf_counter() - started

        # Coordinates nothing warmed, rather than clearing the cache: sessions are `cached_db`, so
        # emptying the cache sends every later request back to the database for its session and
        # measures that instead of the refusal.
        cold = [
            reverse("map.basemap_tiles", kwargs={"layer": "street", "z": 12, "x": 1300 + index, "y": 1539})
            for index in range(VIEWPORT_TILES)
        ]
        with mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", 1):
            basemap_tiles.UpstreamSlots.reset()
            semaphore = basemap_tiles.UpstreamSlots.semaphore()
            self.assertTrue(semaphore.acquire(blocking=False))
            try:
                with mock.patch(_CONFIGURED, return_value=True):
                    started = time.perf_counter()
                    with CaptureQueriesContext(connection) as refusing:
                        statuses = [self.client.get(url).status_code for url in cold]
                    refused = time.perf_counter() - started
            finally:
                semaphore.release()
        self.assertEqual(set(statuses), {503}, "the measurement above must be of refusals, not of served tiles")

        print(
            f"\n  viewport served: {len(serving.captured_queries)} queries, {served * 1000:.0f} ms; "
            f"refused: {len(refusing.captured_queries)} queries, {refused * 1000:.0f} ms"
        )
        self.assertLessEqual(len(refusing.captured_queries), len(serving.captured_queries))
