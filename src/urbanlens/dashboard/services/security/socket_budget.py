"""How many WebSocket connections one account may hold open at once.

Authorization on these sockets is thorough and ``InboundVolumeMixin`` bounds how
fast an account may *send* on them. Neither bounds how many it may *hold*. An
idle socket sends nothing, so it is charged nothing, while still occupying one of
nginx's ``worker_connections`` (1024 per worker, shared with every HTTP request)
and a slot in the single daphne process behind it. One account opening a few
thousand costs every other user their connection - which is the availability
requirement failing at the cheapest possible price to the attacker (N21 H10).

Held in a ``services.core.connection_registry.ConnectionRegistry``: a sorted set
per identity, member per connection. A worker that dies mid-connection leaves
members that age out on their own, so the worst a crash costs is a smaller
allowance until ``STALE_AFTER_SECONDS`` passes.

**Fails open.** A cap that cannot read its set allows: a Dragonfly outage
already degrades the site, and "nobody may open a socket" would make it worse.
The channel layer lives on the same store, so during that outage the sockets
this admits cannot join a group or fan out either.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings

from urbanlens.dashboard.services.core.connection_registry import ConnectionRegistry, store_client

if TYPE_CHECKING:
    import redis

logger = logging.getLogger(__name__)

#: How long a claim counts without being renewed. A live socket renews every
#: :data:`REFRESH_INTERVAL_SECONDS`, so this only ever expires the claims of a
#: worker that went away - and it is deliberately short, because until it does
#: those claims cost the account part of its allowance.
STALE_AFTER_SECONDS = 15 * 60

#: How often a live connection renews its claim. Three renewals fit inside
#: :data:`STALE_AFTER_SECONDS`, so a missed tick is survivable.
REFRESH_INTERVAL_SECONDS = 5 * 60


def max_sockets_per_account() -> int:
    """The most sockets one account may hold at once.

    Read at call time rather than bound at import, so a test can lower it
    without opening twenty connections.

    Returns:
        The configured ceiling.
    """
    return int(getattr(settings, "WEBSOCKET_MAX_SOCKETS_PER_ACCOUNT", 20))


def _client() -> redis.Redis | None:
    """The store client; the seam tests substitute."""
    return store_client()


def _current_client() -> redis.Redis | None:
    # Resolves `_client` per call, so substituting it reaches the registry.
    return _client()


_registry = ConnectionRegistry(prefix="ul_ws_open", stale_after_seconds=STALE_AFTER_SECONDS, client=_current_client)


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
    open_now = _registry.add(identity, connection_id)
    if open_now is None or open_now <= limit:
        return True
    # Counted with this one included, so being over means this is the one that
    # would exceed it - taken back out, or a refused connection would go on
    # occupying the allowance it was refused for.
    release(identity, connection_id)
    logger.info("Refused a socket for %s: %s already open, at a ceiling of %s", identity, open_now - 1, limit)
    return False


def release(identity: str, connection_id: str) -> None:
    """Give one connection's place back.

    Args:
        identity: Who it was charged to.
        connection_id: The same value :func:`claim` was given.
    """
    _registry.remove(identity, connection_id)


def refresh(identity: str, connection_id: str) -> None:
    """Renew a live connection's claim so it is not swept as abandoned.

    Args:
        identity: Who it is charged to.
        connection_id: The same value :func:`claim` was given.
    """
    _registry.refresh(identity, connection_id)


def open_count(identity: str) -> int:
    """How many live connections one identity holds, for tests and diagnostics.

    Args:
        identity: Whose connections to count.

    Returns:
        The count, or 0 when the store cannot answer.
    """
    return _registry.count(identity) or 0
