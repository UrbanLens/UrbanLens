"""A local HTTP(S) server that answers one byte at a time, for testing wall-clock deadlines on real sockets."""

from __future__ import annotations

import datetime
import ipaddress
import os
import shutil
import socketserver
import ssl
import tempfile
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
    tls: ssl.SSLContext | None = None

    def handle(self) -> None:
        try:
            conn = self.tls.wrap_socket(self.request, server_side=True) if self.tls is not None else self.request
            conn.recv(65536)
            if self.drip_headers:
                self._drip(conn, _HEAD + self.body)
                return
            conn.sendall(_HEAD)
            self._drip(conn, self.body)
        except (OSError, ssl.SSLError):
            return

    def _drip(self, conn, payload: bytes) -> None:
        for byte in payload:
            if self.stop.is_set():
                return
            conn.sendall(bytes([byte]))
            time.sleep(self.interval)


def _self_signed_context(test: TestCase) -> ssl.SSLContext:
    """A server context with a throwaway certificate for 127.0.0.1."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    directory = tempfile.mkdtemp()
    cert_path, key_path = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as out:
        out.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as out:
        out.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def start_drip_server(
    test: TestCase, *, drip_headers: bool = False, interval: float = 0.1, body_bytes: int = 1000, tls: bool = False
) -> str:
    """Serve a slow-drip response on loopback for the rest of *test*.

    Args:
        test: The running test; the server is stopped in its cleanups.
        drip_headers: Drip the status line and headers too, not only the body.
        interval: Seconds between bytes.
        body_bytes: Length of the body.
        tls: Serve HTTPS with a throwaway self-signed certificate; the client must not verify it.

    Returns:
        The server's base url, ``http(s)://127.0.0.1:<port>/``.
    """
    stop = threading.Event()
    attributes = {
        "drip_headers": drip_headers,
        "interval": interval,
        "body": b"b" * body_bytes,
        "stop": stop,
        "tls": _self_signed_context(test) if tls else None,
    }
    handler = type("Handler", (_DripHandler,), attributes)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 5)
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    test.addCleanup(stop.set)
    scheme = "https" if tls else "http"
    return f"{scheme}://127.0.0.1:{server.server_address[1]}/"
