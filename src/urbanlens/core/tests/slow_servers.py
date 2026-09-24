"""A local HTTP server that answers one byte at a time, for testing wall-clock deadlines on real sockets."""

from __future__ import annotations

import socketserver
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unittest import TestCase

_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nX-Padding: " + b"a" * 200 + b"\r\n\r\n"


class _DripHandler(socketserver.BaseRequestHandler):
    """Sends its response a byte per ``interval``, each byte well inside any read timeout."""

    drip_headers = False
    interval = 0.1
    body = b"b" * 1000
    stop: threading.Event

    def handle(self) -> None:
        self.request.recv(65536)
        try:
            if self.drip_headers:
                self._drip(_HEAD + self.body)
                return
            self.request.sendall(_HEAD)
            self._drip(self.body)
        except OSError:
            return

    def _drip(self, payload: bytes) -> None:
        for byte in payload:
            if self.stop.is_set():
                return
            self.request.sendall(bytes([byte]))
            time.sleep(self.interval)


def start_drip_server(
    test: TestCase, *, drip_headers: bool = False, interval: float = 0.1, body_bytes: int = 1000
) -> str:
    """Serve a slow-drip response on loopback for the rest of *test*.

    Args:
        test: The running test; the server is stopped in its cleanups.
        drip_headers: Drip the status line and headers too, not only the body.
        interval: Seconds between bytes.
        body_bytes: Length of the body.

    Returns:
        The server's base url, ``http://127.0.0.1:<port>/``.
    """
    stop = threading.Event()
    handler = type(
        "Handler",
        (_DripHandler,),
        {"drip_headers": drip_headers, "interval": interval, "body": b"b" * body_bytes, "stop": stop},
    )
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 5)
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    test.addCleanup(stop.set)
    return f"http://127.0.0.1:{server.server_address[1]}/"
