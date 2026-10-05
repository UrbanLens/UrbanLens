"""One compressed shard of a downloadable building dataset, read within a size cap.

Google's Open Buildings and Microsoft's footprints publish their buildings as gzip shards that run to gigabytes where
the data is dense, so a shard is read only up to a cap, and one that is missing or past it is not asked for again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.cache import DEFAULT_CACHE_ALIAS

from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_or_skip
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, read_capped

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)

#: A shard that is missing or too large stays so for as long as a dataset's release does.
UNUSABLE_SHARD_SECONDS = 30 * 24 * 60 * 60


def read_shard(session: requests.Session, url: str, *, max_bytes: int, what: str) -> bytes | None:
    """A shard's compressed body, or None when it is missing or past *max_bytes*; either is remembered.

    Runs inside the boundary panel-fetch Celery task, whose soft time limit is the real budget; the (connect, read)
    timeout keeps a dead connection from spending it.

    Args:
        session: The gateway's session.
        url: The shard's URL.
        max_bytes: The most compressed bytes to read.
        what: The shard's kind, for logs and errors.

    Returns:
        The compressed bytes, or None.

    Raises:
        requests.RequestException: The request failed for another reason, which is not remembered.
    """
    unusable_key = f"boundary-shard:unusable:{url}"
    if get_or_none(unusable_key, label=what, alias=DEFAULT_CACHE_ALIAS):
        return None
    response = session.get(url, timeout=(5, 60), stream=True)
    try:
        if response.status_code == 404:
            reason = "missing"
        else:
            response.raise_for_status()
            length = response.headers.get("Content-Length", "")
            reason = "too large" if length.isdigit() and int(length) > max_bytes else ""
        if not reason:
            try:
                return read_capped(response, max_bytes=max_bytes, what=what)
            except GatewayRequestError:
                reason = "too large"
    finally:
        response.close()
    if reason == "too large":
        logger.info("Skipping %s at %s: it is larger than %s bytes", what, url, max_bytes)
    set_or_skip(unusable_key, reason, UNUSABLE_SHARD_SECONDS, label=what, alias=DEFAULT_CACHE_ALIAS)
    return None
