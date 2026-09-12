"""A gateway proxying someone else's media must bound what it reads into memory.

`bounded_cache.set_if_small` already stops an oversized body being *stored* in
the shared Valkey (H14, H22, H35). Nothing stopped it being *read*: every media
download in the REData gateways was `response.content`, which buffers the whole
body into the web worker regardless of size. The throttle in front of these
endpoints bounds how often that happens, not how big it gets, and under gevent a
worker killed for memory takes every other in-flight request on it with it.

The trap this guards is the reason the cap is not simply a `len()` check after
the fact. `requests` reads the entire body during `send()` unless the request
was made with `stream=True` - so a cap applied to an already-buffered response
measures memory that has *already* been spent, while `response.raw.read(...)` on
one returns an empty body and looks like a successful download of nothing.
`read_capped` therefore refuses to guess: it raises when handed a response that
was not streamed, so the mistake is loud at the call site rather than silent in
production.
"""

from __future__ import annotations

import io

import pytest
import requests
from urllib3 import HTTPResponse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.gateway import MAX_PROXIED_MEDIA_BYTES, GatewayRequestError, read_capped


def _streamed(body: bytes, content_type: str = "image/jpeg") -> requests.Response:
    """A response shaped like one requests returns for `stream=True`."""
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = content_type
    response.raw = HTTPResponse(
        body=io.BytesIO(body), headers={"Content-Type": content_type}, status=200, preload_content=False
    )
    response._content_consumed = False
    return response


def _buffered(body: bytes) -> requests.Response:
    """A response shaped like one requests returns WITHOUT `stream=True`."""
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "image/jpeg"
    response._content = body
    response._content_consumed = True
    response.raw = None
    return response


class TheCapIsEnforcedTests(SimpleTestCase):
    def test_a_small_body_comes_through_whole(self) -> None:
        """Anti-vacuity: a helper that refused everything would pass the test
        below while breaking every gallery on the site."""
        body = b"x" * 1024

        self.assertEqual(read_capped(_streamed(body), max_bytes=4096, what="photo"), body)

    def test_a_body_at_the_cap_is_still_served(self) -> None:
        """The boundary belongs to the caller, not the refusal."""
        body = b"x" * 4096

        self.assertEqual(read_capped(_streamed(body), max_bytes=4096, what="photo"), body)

    def test_a_body_over_the_cap_is_refused(self) -> None:
        with pytest.raises(GatewayRequestError, match="larger than"):
            read_capped(_streamed(b"x" * 4097), max_bytes=4096, what="photo")

    def test_the_refusal_does_not_read_the_whole_body(self) -> None:
        """The point is the memory never being spent, so the helper must stop
        at the cap rather than read everything and then complain."""
        raw = io.BytesIO(b"x" * 10_000_000)
        response = _streamed(b"")
        response.raw = HTTPResponse(body=raw, headers={}, status=200, preload_content=False)

        with pytest.raises(GatewayRequestError):
            read_capped(response, max_bytes=4096, what="photo")

        self.assertLessEqual(raw.tell(), 4096 * 4, "the helper buffered far more than the cap before refusing")


class TheNonStreamedMistakeIsLoudTests(SimpleTestCase):
    """A cap that silently does nothing is worse than no cap, because it reads
    as protection in review."""

    def test_a_buffered_response_is_refused_rather_than_read_as_empty(self) -> None:
        with pytest.raises(GatewayRequestError, match="stream=True"):
            read_capped(_buffered(b"x" * 10), max_bytes=4096, what="photo")


class TheDefaultIsSaneTests(SimpleTestCase):
    def test_the_default_cap_is_bounded_and_generous(self) -> None:
        """Large enough for a real photo or map tile, small enough that a worker
        cannot be pushed over its mem_limit by one request."""
        self.assertGreaterEqual(MAX_PROXIED_MEDIA_BYTES, 8 * 1024 * 1024)
        self.assertLessEqual(MAX_PROXIED_MEDIA_BYTES, 64 * 1024 * 1024)


class TheGatewaysActuallyUseItTests(SimpleTestCase):
    """The existing proxy tests stub `download_listing_photo` itself, so they
    pass whatever the gateway does underneath - including against a cap that was
    never wired in, or one wired in wrongly enough to serve empty bodies. These
    stub the HTTP layer instead, which is the only level at which `stream=True`
    and `read_capped` are observable.
    """

    def _gateway(self, body: bytes):
        """A REData gateway whose session returns *body* as a streamed response."""
        from unittest import mock

        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        gateway = RedataGateway(base_url="https://redata.example")
        session = mock.Mock()
        session.get.return_value = _streamed(body)
        gateway.session = session
        return gateway, session

    def test_a_normal_photo_still_downloads(self) -> None:
        """The half that catches a broken cap: `raw.read()` on a response the
        gateway forgot to stream returns b'', which is a silent empty image."""
        gateway, session = self._gateway(b"x" * 2048)

        content, content_type = gateway.download_listing_photo("abc", 1)

        self.assertEqual(content, b"x" * 2048)
        self.assertEqual(content_type, "image/jpeg")
        self.assertTrue(
            session.get.call_args.kwargs.get("stream"), "the media GET was not streamed, so its size cannot be bounded"
        )

    def test_an_oversized_photo_is_refused(self) -> None:
        gateway, _ = self._gateway(b"x" * (MAX_PROXIED_MEDIA_BYTES + 1))

        with pytest.raises(GatewayRequestError, match="larger than"):
            gateway.download_listing_photo("abc", 1)

    def test_every_media_download_streams(self) -> None:
        """One forgotten `stream=True` is a silent empty body, so this asserts
        the property across the gateways rather than per call site."""
        import inspect
        import re

        from urbanlens.dashboard.services.apis.locations import (
            redata_basemap_tiles_gateway,
            redata_historical_maps_gateway,
            redata_imagery_gateway,
        )
        from urbanlens.dashboard.services.apis.property_records import redata_gateway

        for module in (
            redata_gateway,
            redata_basemap_tiles_gateway,
            redata_historical_maps_gateway,
            redata_imagery_gateway,
        ):
            source = inspect.getsource(module)
            for call in re.findall(r"read_capped\(\s*response[^)]*\)", source):
                self.assertIn("response", call)
            # Every capped read must have a streamed GET somewhere in its module.
            if "read_capped(" in source:
                self.assertIn("stream=True", source, f"{module.__name__} caps a read but never streams a request")
