"""How many WebSocket connections one account may hold open at once.

Authorization on these sockets is thorough and ``InboundVolumeMixin`` bounds how
fast an account may *send* on them. Neither bounds how many it may *hold*. An
idle socket sends nothing, so it is charged nothing, while still occupying one of
nginx's ``worker_connections`` (1024 per worker, shared with every HTTP request)
and a slot in the single daphne process behind it. One account opening a few
thousand costs every other user their connection - which is the availability
requirement failing at the cheapest possible price to the attacker (N21 H10).

Held as a sorted set per identity, member per connection, score the time it was
claimed. That shape is chosen for what happens when a worker dies mid-connection:
a plain counter would be incremented and never decremented, and the account would
be locked out of its own sockets until somebody noticed. Here the leftovers age
out of the set on their own, so the worst a crash costs is a smaller allowance
until ``STALE_AFTER_SECONDS`` passes.

**Fails open.** A cap that cannot read its counter must allow, for the same
reason the request throttle does: a Valkey outage already degrades the site, and
turning it into "nobody may open a socket" makes an outage worse rather than
safer. The asymmetry is deliberate and is the opposite of the single-flight
guard, where proceeding blind starts a second copy of the most expensive work.
"""

from __future__ import annotations

import logging
import os
import time

from django.conf import settings
import redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

#: Anything the store can raise when it cannot answer. RuntimeError is the test
#: suite's own network guard, which must read as "no counter", not as an error.
_STORE_ERRORS = (RedisError, ConnectionError, OSError, RuntimeError)

#: How long a claim counts without being renewed. A live socket renews every
#: :data:`REFRESH_INTERVAL_SECONDS`, so this only ever expires the claims of a
#: worker that went away - and it is deliberately short, because until it does
#: those claims cost the account part of its allowance.
#:
#: These sockets live as long as their tab, which is hours. Without the renewal
#: this would have to be hours too, and then a crash would cost an account most
#: of its allowance for most of a day; with it, three renewals fit inside the
#: window, so a missed tick is survivable and a dead worker clears in minutes.
STALE_AFTER_SECONDS = 15 * 60

#: How often a live connection renews its claim. Comfortably inside
#: :data:`STALE_AFTER_SECONDS` - one ZADD per socket per interval, which for a
#: thousand sockets is a few writes a second.
REFRESH_INTERVAL_SECONDS = 5 * 60


def max_sockets_per_account() -> int:
    """The most sockets one account may hold at once.

    Read at call time rather than bound at import, so a test can lower it
    without opening twenty connections.

    Returns:
        The configured ceiling.
    """
    return int(getattr(settings, "WEBSOCKET_MAX_SOCKETS_PER_ACCOUNT", 20))


#: Built once and shared. `from_url` makes a new connection pool each call, and
#: this runs on every socket connect *and* disconnect - the churn a chat page
#: produces would otherwise be a new TCP connection per event. redis-py's client
#: is thread-safe and pools internally, which is what makes sharing it correct
#: rather than merely cheaper.
_shared_client: redis.Redis | None = None
_client_built = False


def _client() -> redis.Redis | None:
    """A raw client, or None when no address is configured.

    Returns:
        The client. Raw rather than through Django's cache API because this
        needs a sorted set, which that API does not expose.
    """
    global _shared_client, _client_built  # noqa: PLW0603 # one lazily-built shared pool, as the note above explains
    if _client_built:
        return _shared_client
    url = os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL")
    _shared_client = redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=1, socket_timeout=2) if url else None
    _client_built = True
    return _shared_client


def _key(identity: str) -> str:
    """The sorted-set key holding one identity's open connections."""
    return f"ul_ws_open:{identity}"


def claim(identity: str, connection_id: str, limit: int | None = None) -> bool:
    """Register one connection and report whether it is within the allowance.

    Args:
        identity: Who the connection is charged to. Must be derived from what
            the server resolved, never from client-supplied text, or a caller
            could spend somebody else's allowance or evade their own.
        connection_id: Unique per connection; Channels' ``channel_name``.
        limit: The most to allow. Defaults to
            :func:`max_sockets_per_account`, read at call time.

    Returns:
        True when the connection may proceed - including when the store cannot
        be reached, which is the fail-open case this module's docstring
        explains. False only when the allowance is definitely spent, in which
        case nothing was left registered.
    """
    limit = max_sockets_per_account() if limit is None else limit
    client = _client()
    if client is None:
        return True
    key = _key(identity)
    now = time.time()
    try:
        pipeline = client.pipeline()
        # Sweep before counting, so one identity's stale entries are cleared by
        # its own next connection rather than needing a sweeper of their own.
        pipeline.zremrangebyscore(key, 0, now - STALE_AFTER_SECONDS)
        pipeline.zadd(key, {connection_id: now})
        pipeline.zcard(key)
        pipeline.expire(key, STALE_AFTER_SECONDS * 2)
        open_now = int(pipeline.execute()[2])
    except _STORE_ERRORS:
        logger.warning("Could not read the socket allowance for %s; allowing the connection", identity, exc_info=True)
        return True
    if open_now <= limit:
        return True
    # Counted with this one included, so being over means this is the one that
    # would exceed it - and it is taken back out rather than left to expire,
    # or a refused connection would go on occupying the allowance it was
    # refused for.
    release(identity, connection_id)
    logger.info("Refused a socket for %s: %s already open, at a ceiling of %s", identity, open_now - 1, limit)
    return False


def release(identity: str, connection_id: str) -> None:
    """Give one connection's place back.

    Args:
        identity: Who it was charged to.
        connection_id: The same value :func:`claim` was given.
    """
    client = _client()
    if client is None:
        return
    try:
        client.zrem(_key(identity), connection_id)
    except _STORE_ERRORS:
        # Left to age out. Nothing to recover here, and raising would turn a
        # store blip into a failed disconnect.
        logger.warning("Could not release the socket claim for %s", identity, exc_info=True)


def refresh(identity: str, connection_id: str) -> None:
    """Renew a live connection's claim so it is not swept as abandoned.

    Only renews what is already there: a claim that has already been swept is
    not re-added, because re-adding it would let a connection that lost its place
    take a new one without being counted against the allowance.

    Args:
        identity: Who it is charged to.
        connection_id: The same value :func:`claim` was given.
    """
    client = _client()
    if client is None:
        return
    try:
        client.zadd(_key(identity), {connection_id: time.time()}, xx=True)
        client.expire(_key(identity), STALE_AFTER_SECONDS * 2)
    except _STORE_ERRORS:
        logger.warning("Could not renew the socket claim for %s", identity, exc_info=True)


def open_count(identity: str) -> int:
    """How many live connections one identity holds, for tests and diagnostics.

    Args:
        identity: Whose connections to count.

    Returns:
        The count, or 0 when the store cannot answer.
    """
    client = _client()
    if client is None:
        return 0
    try:
        client.zremrangebyscore(_key(identity), 0, time.time() - STALE_AFTER_SECONDS)
        return int(client.zcard(_key(identity)))
    except _STORE_ERRORS:
        return 0
