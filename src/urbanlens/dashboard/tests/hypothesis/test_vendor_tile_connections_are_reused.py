"""Consecutive vendor tiles share a connection instead of opening one each.

The gateway is built once per uncached tile, and each one used to bring a fresh ``requests.Session`` - so every tile
paid a TCP and TLS handshake to Esri before asking for anything. Against a real local server here, so the connection
count is what a socket actually saw.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations.basemap_vendor_tiles_gateway import BasemapVendorTilesGateway
from urbanlens.dashboard.services.core.gateway import pooled_session
from urbanlens.dashboard.services.map.basemap_vendors import VENDOR_TILES, VendorTiles

_TILE = b"\x89PNG tile"


class _TileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _CountingServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        self.server.request_headers.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(_TILE)))
        self.send_header("Set-Cookie", "vendor_tracker=1; Path=/")
        self.end_headers()
        self.wfile.write(_TILE)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's own name
        return


class _CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _TileHandler)
        self.connections = 0
        self.request_headers: list[dict[str, str]] = []

    def get_request(self) -> tuple[socket.socket, object]:
        accepted = super().get_request()
        self.connections += 1
        return accepted


@contextlib.contextmanager
def local_vendor() -> Iterator[_CountingServer]:
    """A vendor on 127.0.0.1, standing in for every raster layer's endpoint."""
    server = _CountingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    template = f"http://127.0.0.1:{server.server_address[1]}/tile/{{z}}/{{y}}/{{x}}"
    try:
        with mock.patch.dict(VENDOR_TILES, {layer: VendorTiles(url_template=template) for layer in VENDOR_TILES}):
            yield server
    finally:
        server.shutdown()
        server.server_close()


class ConsecutiveTilesShareAConnectionTests(TestCase):
    def test_three_tiles_one_connection(self) -> None:
        with local_vendor() as vendor:
            statuses = [BasemapVendorTilesGateway().download_tile("satellite", 4, x, 1)[0] for x in range(3)]

        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(len(vendor.request_headers), 3)
        self.assertEqual(vendor.connections, 1)

    def test_the_vendor_is_handed_back_no_cookie(self) -> None:
        """The session outlives the gateway; only the connection may carry over, never anything the vendor set."""
        with local_vendor() as vendor:
            for x in range(2):
                BasemapVendorTilesGateway().download_tile("satellite", 4, x, 1)

        self.assertNotIn("Cookie", vendor.request_headers[1])


class PooledSessionTests(SimpleTestCase):
    def test_one_thread_gets_the_same_session_back(self) -> None:
        self.assertIs(pooled_session("basemap_vendor_tiles"), pooled_session("basemap_vendor_tiles"))

    def test_each_service_and_each_thread_has_its_own(self) -> None:
        other_thread: list[object] = []
        worker = threading.Thread(target=lambda: other_thread.append(pooled_session("basemap_vendor_tiles")))
        worker.start()
        worker.join()

        self.assertIsNot(other_thread[0], pooled_session("basemap_vendor_tiles"))
        self.assertIsNot(pooled_session("some_other_service"), pooled_session("basemap_vendor_tiles"))

    def test_only_a_gateway_that_asks_shares_one(self) -> None:
        from urbanlens.dashboard.services.apis.locations.protomaps_basemap_gateway import ProtomapsBasemapGateway

        self.assertIs(BasemapVendorTilesGateway().session._session, BasemapVendorTilesGateway().session._session)
        self.assertIsNot(ProtomapsBasemapGateway().session._session, ProtomapsBasemapGateway().session._session)
