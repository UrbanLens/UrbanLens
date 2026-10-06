"""How long an uncached basemap tile takes, on a live server, against a local stand-in for the vendor.

Production's slow-request log on 0.8.0 had a satellite miss at a median 1.44s of wall time and 1.11s of SQL, with
seven queries each, while Esri answered in 0.09-0.33s. These measure the same request through the whole stack -
middleware, the session, the proxy, the rate limiter, a real socket to a vendor that takes 150ms - with twenty
misses in flight at once, as a cold viewport sends them.

Measured and reported rather than asserted, as the other cost tests here are: the host is shared, and a timing
assertion would fail for reasons that have nothing to do with the code. What is asserted is deterministic - every
tile is served, and each reached the vendor once. Run with ``-s`` to read the figures. ``_DB_ROUND_TRIP_SECONDS``
adds a fixed delay to every query, standing in for the network between a pod and its pooled Postgres that a local
test database does not have.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import statistics
import threading
import time
from typing import Any
from unittest import mock
import urllib.error
import urllib.request

from django.contrib.auth.models import User
from django.core.cache import caches
from django.core.servers.basehttp import ThreadedWSGIServer
from django.db import connections
from django.db.backends.signals import connection_created
from django.test import LiveServerTestCase
from django.test.testcases import LiveServerThread
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.controllers import basemap_tiles
from urbanlens.dashboard.services.core import call_tally, provider_health
from urbanlens.dashboard.services.core.counters import reset_local_fallback
from urbanlens.dashboard.services.map.basemap_vendors import VENDOR_TILES, VendorTiles

_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"
#: What Esri took per tile from production, near the low end of 0.09-0.33s.
_VENDOR_SECONDS = 0.15
_TILE = b"\xff\xd8\xff" + b"j" * 12_000
_MISSES = 20
_DB_ROUND_TRIPS = (0.0, 0.002)
#: Request threads: one production pod, two gunicorn workers of four threads each.
_REQUEST_THREADS = 8


class _PooledWSGIServer(ThreadedWSGIServer):
    """Request threads from a fixed pool, as gunicorn's gthread worker keeps them, rather than a new one per request.

    It matters to what is measured: a connection a request thread keeps open is only reused by a later request on
    the same thread.
    """

    request_queue_size = 64

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pool = ThreadPoolExecutor(max_workers=_REQUEST_THREADS)

    def process_request(self, request: Any, client_address: Any) -> None:
        self._pool.submit(self.process_request_thread, request, client_address)

    def server_close(self) -> None:
        super().server_close()
        self._pool.shutdown(wait=False)


class _PooledLiveServerThread(LiveServerThread):
    server_class = _PooledWSGIServer


class _SlowVendor(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _VendorServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        time.sleep(_VENDOR_SECONDS)
        with self.server.lock:
            self.server.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(_TILE)))
        self.end_headers()
        self.wfile.write(_TILE)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's own name
        return


class _VendorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SlowVendor)
        self.lock = threading.Lock()
        self.connections = 0
        self.requests = 0

    def get_request(self) -> tuple[socket.socket, Any]:
        accepted = super().get_request()
        with self.lock:
            self.connections += 1
        return accepted


@contextlib.contextmanager
def _vendor() -> Iterator[_VendorServer]:
    server = _VendorServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    template = f"http://127.0.0.1:{server.server_address[1]}/tile/{{z}}/{{y}}/{{x}}"
    try:
        with mock.patch.dict(VENDOR_TILES, {layer: VendorTiles(url_template=template) for layer in VENDOR_TILES}):
            yield server
    finally:
        server.shutdown()
        server.server_close()


class _QueryMeter:
    """Counts every query any thread runs, and delays each by a fixed round trip."""

    def __init__(self, round_trip: float) -> None:
        self.round_trip = round_trip
        self.lock = threading.Lock()
        self.statements: list[str] = []

    def __call__(self, execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        with self.lock:
            self.statements.append(sql)
        if self.round_trip:
            time.sleep(self.round_trip)
        return execute(sql, params, many, context)

    def attach(self, sender: object, connection: Any, **_kwargs: object) -> None:
        connection.execute_wrappers.append(self)


class MissLatencyTests(LiveServerTestCase):
    server_thread_class = _PooledLiveServerThread

    def setUp(self) -> None:
        super().setUp()
        for alias in caches:
            caches[alias].clear()
        reset_local_fallback()
        provider_health.forget_snapshot()
        call_tally.forget_limits()
        baker.make(User)
        self.client.force_login(baker.make(User))
        self.cookie = f"sessionid={self.client.cookies['sessionid'].value}"
        basemap_tiles.UpstreamSlots.reset()
        self.addCleanup(basemap_tiles.UpstreamSlots.reset)

    def _get(self, x: int, y: int) -> tuple[int, float]:
        url = self.live_server_url + reverse(
            "map.basemap_tiles", kwargs={"layer": "satellite", "z": 12, "x": x, "y": y}
        )
        request = urllib.request.Request(url, headers={"Cookie": self.cookie})  # noqa: S310 - the live test server
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - the live test server
                response.read()
                status = response.status
        except urllib.error.HTTPError as refusal:
            status = refusal.code
        return status, time.perf_counter() - started

    def _burst(self, y: int, count: int) -> tuple[list[tuple[int, float]], float]:
        barrier = threading.Barrier(count)
        results: list[tuple[int, float]] = [(0, 0.0)] * count

        def one(index: int) -> None:
            barrier.wait()
            results[index] = self._get(5000 + index, y)

        threads = [threading.Thread(target=one, args=(index,)) for index in range(count)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        return results, time.perf_counter() - started

    def test_twenty_simultaneous_misses(self) -> None:
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", _MISSES),
            _vendor() as vendor,
        ):
            # One tile first, so the session's first-tile check and the limits read are not in the burst.
            self._get(1, 1)
            for row, round_trip in enumerate(_DB_ROUND_TRIPS):
                meter = _QueryMeter(round_trip)
                connection_created.connect(meter.attach)
                connections.close_all()
                try:
                    before = vendor.requests
                    connections_before = vendor.connections
                    results, wall = self._burst(100 + row, _MISSES)
                finally:
                    connection_created.disconnect(meter.attach)
                    connections.close_all()
                times = sorted(elapsed for _, elapsed in results)
                print(
                    f"\n  {_MISSES} simultaneous misses, vendor {_VENDOR_SECONDS * 1000:.0f} ms, db round trip {round_trip * 1000:.0f} ms: "
                    f"p50 {statistics.median(times) * 1000:.0f} ms, p95 {times[int(len(times) * 0.95) - 1] * 1000:.0f} ms, "
                    f"max {times[-1] * 1000:.0f} ms, burst {wall * 1000:.0f} ms; "
                    f"{len(meter.statements) / _MISSES:.1f} queries per miss, {_REQUEST_THREADS} request threads, "
                    f"{vendor.connections - connections_before} new vendor connections"
                )
                locked = [sql for sql in meter.statements if "FOR UPDATE" in sql.upper()]
                print(f"    locking statements: {len(locked)}")
                self.assertEqual([status for status, _ in results], [200] * _MISSES)
                self.assertEqual(vendor.requests - before, _MISSES)

    def test_a_cold_viewport_through_the_browsers_queue(self) -> None:
        """The client paces own tiles through ``OWN_TILE_CONCURRENCY`` slots (``own-tiles.ts``) and retries a 503
        after a second; this replays a 30-tile viewport that way against the server's default slot count, at a
        few queue widths, to see whether a wider queue would be served or refused."""
        for width in (6, 12):
            with mock.patch(_CONFIGURED, return_value=True), _vendor() as vendor:
                self._get(1, 1)
                started = time.perf_counter()
                refusals = self._paced_viewport(width, 30, y=200 + width)
                elapsed = time.perf_counter() - started
            print(
                f"\n  30-tile cold viewport, queue {width} wide, {basemap_tiles.app_settings.basemap_tile_upstream_concurrency} upstream slots: {elapsed * 1000:.0f} ms, {refusals} refusals, {vendor.requests} vendor calls"
            )

    def _paced_viewport(self, width: int, tiles: int, *, y: int) -> int:
        queue = threading.Semaphore(width)
        lock = threading.Lock()
        refusals = 0

        def one(x: int) -> None:
            nonlocal refusals
            for _attempt in range(5):
                with queue:
                    status, _ = self._get(6000 + x, y)
                if status != 503:
                    return
                with lock:
                    refusals += 1
                time.sleep(1.0)

        threads = [threading.Thread(target=one, args=(x,)) for x in range(tiles)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        return refusals
