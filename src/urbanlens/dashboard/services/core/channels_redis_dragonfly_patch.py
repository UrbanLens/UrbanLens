"""Make channels_redis's backup-queue script declare its keys, so Dragonfly stops rejecting it.

``RedisChannelLayer._brpop_with_clean`` (the receive-side of every WebSocket consumer) runs a Lua
script that references two keys through ``ARGV`` instead of declaring them via ``KEYS``. Standalone
Redis tolerates that outside cluster mode; Dragonfly enforces the cluster rule unconditionally and
raises ``script tried accessing undeclared key`` on every call, crashing the receive loop (see P127).
Declaring the same two values as ``KEYS[1]``/``KEYS[2]`` instead of ``ARGV[1]``/``ARGV[2]`` changes
nothing about what the script does - only whether the server can see which keys it touches - so this
is silent under real Redis and just fixes Dragonfly, with no per-script server flag required.

Upstream tracks this as django/channels_redis#345, unresolved as of channels-redis 4.3.0 (this
project's pin). Revert this module once that lands.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The channels-redis versions this patch's replacement body has been checked against. A version
#: outside this set may have already changed `_brpop_with_clean` underneath us - better to run
#: unpatched (and crash loudly against Dragonfly, which is at least diagnosable) than to silently
#: reinstate a since-fixed or since-changed bug with a stale copy.
_VERIFIED_VERSIONS = frozenset({"4.3.0"})

_PATCHED_MARKER = "_urbanlens_dragonfly_safe"


def patch_backup_queue_script() -> None:
    """Replace ``RedisChannelLayer._brpop_with_clean`` with a cluster/Dragonfly-safe version.

    No-op (with a logged warning) when the installed channels-redis version has not been checked
    against this patch's copy of the method, and idempotent on repeated calls.
    """
    import channels_redis
    from channels_redis import core

    if getattr(core.RedisChannelLayer._brpop_with_clean, _PATCHED_MARKER, False):  # noqa: SLF001 - patching this exact private method
        return

    if channels_redis.__version__ not in _VERIFIED_VERSIONS:
        logger.warning(
            "channels_redis %s has not been checked against the Dragonfly key-declaration patch; running unpatched.",
            channels_redis.__version__,
        )
        return

    cleanup_script = """
        local backed_up = redis.call('ZRANGE', KEYS[2], 0, -1, 'WITHSCORES')
        for i = #backed_up, 1, -2 do
            redis.call('ZADD', KEYS[1], backed_up[i], backed_up[i - 1])
        end
        redis.call('DEL', KEYS[2])
    """

    async def _brpop_with_clean_dragonfly_safe(
        self: core.RedisChannelLayer,
        index: int,
        channel: str,
        timeout: int,  # noqa: ASYNC109 - matching upstream's own signature, which callers pass a bare int to
    ):
        backup_queue = self._backup_channel_name(channel)
        connection = self.connection(index)
        # Only difference from upstream: numkeys=2 with both keys passed as KEYS instead of ARGV,
        # so a server that validates script key declarations (Dragonfly, Redis Cluster) can see them.
        await connection.eval(cleanup_script, 2, channel, backup_queue)
        result = await connection.bzpopmin(channel, timeout=timeout)

        if result is not None:
            _, member, timestamp = result
            await connection.zadd(backup_queue, {member: float(timestamp)})
        else:
            member = None

        return member

    _brpop_with_clean_dragonfly_safe.__dict__[_PATCHED_MARKER] = True
    core.RedisChannelLayer._brpop_with_clean = _brpop_with_clean_dragonfly_safe  # noqa: SLF001 - the whole point is patching channels_redis's private method
