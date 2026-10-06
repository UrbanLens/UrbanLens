"""A stand-in for the raster vendor's side of the wire, for tests that go through the rate-limited session.

Patching ``download_tile`` skips the session, and with it the limiter this is all about; this patches the request
underneath it instead, so every query and counter the real path makes is still made.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import io
from typing import Any
from unittest import mock

import requests
import urllib3


def vendor_answer(
    status: int = 200, body: bytes = b"\x89PNG tile", content_type: str = "image/png"
) -> requests.Response:
    """One streamed response, as ``requests`` hands it over with ``stream=True``.

    Args:
        status: HTTP status.
        body: The bytes.
        content_type: The declared type.

    Returns:
        A response whose body has not been read.
    """
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response.raw = urllib3.HTTPResponse(body=io.BytesIO(body), status=status, preload_content=False)
    response.url = "https://vendor.test/tile"
    return response


@contextlib.contextmanager
def vendor_wire(answer: Callable[..., requests.Response] | None = None) -> Iterator[mock.MagicMock]:
    """Answer every outbound request with *answer*'s response, or a tile.

    Args:
        answer: Called with the request's ``(session, method, url, **kwargs)``; may raise to stand for a failure.

    Yields:
        The patch, whose ``call_count`` is how many requests reached the wire.
    """

    def respond(*args: Any, **kwargs: Any) -> requests.Response:
        return answer(*args, **kwargs) if answer is not None else vendor_answer()

    with mock.patch.object(requests.Session, "request", autospec=True, side_effect=respond) as wire:
        yield wire
