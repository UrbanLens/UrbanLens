"""Which live connections an identity holds, as a sorted set that forgets dead ones.

One member per connection, scored by when it last renewed. A worker that dies
mid-connection never removes its members, so anything counted as "open" is only
what renewed within ``stale_after_seconds``; the rest ages out without a sweeper.
A plain counter has the opposite failure: a lost decrement is permanent.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
from typing import TYPE_CHECKING

import redis
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Anything the store can raise when it cannot answer. RuntimeError is the test
#: suite's own network guard.
STORE_ERRORS = (RedisError, ConnectionError, OSError, RuntimeError)

_shared_client: redis.Redis | None = None
_client_built = False


def store_client() -> redis.Redis | None:
    """One shared raw client, or None when no store is configured.

    Raw rather than Django's cache API, which has no sorted sets. Shared because
    ``from_url`` builds a pool per call and this runs on every connect and disconnect.

    Returns:
        The client, or None.
    """
    global _shared_client, _client_built  # noqa: PLW0603 - one lazily-built shared pool
    if _client_built:
        return _shared_client
    url = os.getenv("UL_DRAGONFLY_URL") or os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL")
    _shared_client = redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=1, socket_timeout=2) if url else None
    _client_built = True
    return _shared_client


@dataclass(frozen=True, slots=True)
class ConnectionRegistry:
    """Live connections per identity.

    Attributes:
        prefix: Namespaces this registry's keys.
        stale_after_seconds: How long a member counts without renewing.
        client: Returns the store client, or None when there is none. Defaults to
            :func:`store_client`, looked up per call so a test can substitute it.
        refresh_readds: Whether :meth:`refresh` puts back a member that already aged
            out. Off for an allowance, where re-adding would hand a place to a
            connection that was never counted against it; on where nothing is capped.
    """

    prefix: str
    stale_after_seconds: int
    client: Callable[[], redis.Redis | None] | None = None
    refresh_readds: bool = False

    def _store(self) -> redis.Redis | None:
        return self.client() if self.client is not None else store_client()

    def key(self, identity: str) -> str:
        """The sorted-set key for *identity*."""
        return f"{self.prefix}:{identity}"

    def add(self, identity: str, connection_id: str) -> int | None:
        """Register a connection and return how many live ones *identity* now holds.

        Args:
            identity: Who holds it.
            connection_id: Unique per connection.

        Returns:
            The live count including this one, or None when the store cannot answer.
        """
        client = self._store()
        if client is None:
            return None
        key, now = self.key(identity), time.time()
        try:
            pipeline = client.pipeline()
            pipeline.zremrangebyscore(key, 0, now - self.stale_after_seconds)
            pipeline.zadd(key, {connection_id: now})
            pipeline.zcard(key)
            pipeline.expire(key, self.stale_after_seconds * 2)
            return int(pipeline.execute()[2])
        except STORE_ERRORS:
            logger.warning("Could not register connection for %s", key, exc_info=True)
            return None

    def remove(self, identity: str, connection_id: str) -> None:
        """Forget a connection. Left to age out when the store cannot be reached.

        Args:
            identity: Who held it.
            connection_id: The value :meth:`add` was given.
        """
        client = self._store()
        if client is None:
            return
        try:
            client.zrem(self.key(identity), connection_id)
        except STORE_ERRORS:
            logger.warning("Could not remove connection for %s", self.key(identity), exc_info=True)

    def refresh(self, identity: str, connection_id: str) -> None:
        """Renew a live connection so it is not aged out.

        Args:
            identity: Who holds it.
            connection_id: The value :meth:`add` was given.
        """
        client = self._store()
        if client is None:
            return
        key = self.key(identity)
        try:
            client.zadd(key, {connection_id: time.time()}, xx=not self.refresh_readds)
            client.expire(key, self.stale_after_seconds * 2)
        except STORE_ERRORS:
            logger.warning("Could not renew connection for %s", key, exc_info=True)

    def count(self, identity: str) -> int | None:
        """How many live connections *identity* holds.

        Args:
            identity: Whose connections to count.

        Returns:
            The count, or None when the store cannot answer.
        """
        client = self._store()
        if client is None:
            return None
        key = self.key(identity)
        try:
            client.zremrangebyscore(key, 0, time.time() - self.stale_after_seconds)
            return int(client.zcard(key))
        except STORE_ERRORS:
            return None
