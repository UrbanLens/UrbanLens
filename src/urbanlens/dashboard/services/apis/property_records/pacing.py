"""Per-host politeness pacing and retry/backoff shared by every property-records HTTP path.

Per ``docs/property-records-plan.md`` section 3 ("rate-limit aggressively
per-domain ... exponential backoff on 429/503"): the tiered pipeline talks to
~3,000 *different* county-run servers through a handful of shared
``service_key`` budgets, so the central rate limiter alone can't stop one
burst from hammering a single small-county host. Every fetch path in this
package - the Tier 1 ArcGIS/Socrata gateway, the Tier 2/3 scrape engine, and
discovery's endpoint validation - routes through these helpers so the
per-host discipline is identical everywhere rather than re-implemented (and
drifting) per module.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)


def _sleep(seconds: float) -> None:
    """Politeness sleep - skipped under test so paced code paths stay fast to exercise."""
    if getattr(settings, "TESTING", False):
        return
    time.sleep(seconds)

#: Minimum seconds between two requests to the same county server host -
#: "1 req/2-3 sec"; kept at the low end since this also has to survive the
#: central service-level rate limit.
MIN_HOST_INTERVAL_SECONDS = 2.0
_HOST_PACE_CACHE_PREFIX = "proprec:hostpace:"
_HOST_PACE_TTL_SECONDS = 60

#: Exponential backoff on 429/503 - a handful of small county ArcGIS/Socrata
#: instances are genuinely under-provisioned and return these under any load.
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0


def pace_host(url: str) -> None:
    """Sleep just long enough to keep requests to this URL's host politely spaced.

    Uses wall-clock time in the shared Django cache (not ``time.monotonic``,
    whose values are meaningless across processes/machines) so pacing holds
    across web workers and Celery workers sharing one cache backend. The
    computed wait is clamped to the interval so cross-machine clock skew can
    only ever shorten a pause, never stretch it.

    Args:
        url: The URL about to be requested.
    """
    host = urlsplit(url).netloc
    if not host:
        return
    key = f"{_HOST_PACE_CACHE_PREFIX}{host}"
    last = cache.get(key)
    if last is not None:
        wait = MIN_HOST_INTERVAL_SECONDS - (time.time() - float(last))
        if 0 < wait <= MIN_HOST_INTERVAL_SECONDS:
            _sleep(wait)
    cache.set(key, time.time(), _HOST_PACE_TTL_SECONDS)


def request_with_backoff(
    session: Any,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
    stream: bool = False,
) -> requests.Response:
    """Issue one GET/POST with per-host pacing and exponential backoff on 429/503.

    Dispatches through ``session.get``/``session.post`` (not
    ``session.request``) so a ``_RateLimitedSession``'s rate-check/call-log
    wrappers and test doubles keyed on those methods both apply unchanged.

    Args:
        session: A ``requests.Session``-compatible object (usually a
            ``Gateway``'s rate-limited session).
        method: ``"GET"`` or ``"POST"``.
        url: Full request URL.
        params: Query parameters.
        data: Form body (POST only).
        headers: Extra request headers.
        timeout: Per-request timeout in seconds.
        stream: Whether to stream the response body.

    Returns:
        The final response - possibly still a 429/503 when every retry was
        exhausted; callers decide how to classify non-ok statuses.

    Raises:
        requests.exceptions.RequestException: Transport-level failure
            (connection refused, DNS, timeout, ...). Rate-limiter
            cancellations (``RequestCancelledError``) also propagate - they
            are not ``RequestException`` subclasses and must reach the
            enrichment runner intact.
    """
    def _issue() -> requests.Response:
        pace_host(url)
        if method == "POST":
            return session.post(url, params=params, data=data, headers=headers, timeout=timeout, stream=stream)
        return session.get(url, params=params, headers=headers, timeout=timeout, stream=stream)

    for attempt in range(MAX_RETRIES - 1):
        response = _issue()
        if response.status_code not in (429, 503):
            return response
        backoff = BACKOFF_BASE_SECONDS * (2**attempt)
        logger.debug("Property-records host %s returned %s, backing off %.1fs", urlsplit(url).netloc, response.status_code, backoff)
        response.close()
        _sleep(backoff)
    return _issue()
