"""Valkey/Redis-backed cache for authenticated users' map pins."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Protocol, Self

import redis
from redis.exceptions import RedisError

from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.services.map_pins.payload import MapPinPage, MapPinPayloadService

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Keys deleted per command when dropping several profiles' caches at once.
_DELETE_CHUNK = 500


class _SyncPipeline(Protocol):
    """Protocol for the subset of Pipeline methods used by MapPinCache."""

    def hset(self, name: str, key: str | None = ..., value: str | None = ..., mapping: dict[str, Any] | None = ...) -> int: ...
    def hdel(self, name: str, *keys: str) -> int: ...
    def zadd(self, name: str, mapping: dict[str, Any]) -> int: ...
    def zrem(self, name: str, *values: str) -> int: ...
    def delete(self, *names: str) -> int: ...
    def expire(self, name: str, time: int) -> bool: ...
    def execute(self) -> list[Any]: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


class _SyncRedis(Protocol):
    """Protocol for the subset of redis.Redis methods used by MapPinCache.

    redis-py stubs return ``Awaitable[T] | T`` for many methods because the same
    stubs cover both the sync and async clients.  This protocol declares the
    concrete sync return types so that callers in this module are properly typed
    without scattering ``type: ignore`` comments throughout.  The single boundary
    cast lives in ``_make_client``.
    """

    def exists(self, *names: str) -> int: ...
    def zrangebyscore(self, name: str, min_score: str | int, max_score: str | int, start: int = ..., num: int = ...) -> list[str]: ...
    def hmget(self, name: str, keys: list[str]) -> list[str | None]: ...
    def zcard(self, name: str) -> int: ...
    def set(self, name: str, value: str, *, nx: bool = ..., ex: int = ...) -> bool | None: ...
    def pipeline(self, transaction: bool = ...) -> _SyncPipeline: ...
    def hset(self, name: str, key: str | None = ..., value: str | None = ..., mapping: dict[str, Any] | None = ...) -> int: ...
    def hget(self, name: str, key: str) -> str | None: ...
    def zadd(self, name: str, mapping: dict[str, Any]) -> int: ...
    def rename(self, src: str, dst: str) -> bool: ...
    def delete(self, *names: str) -> int: ...
    def hdel(self, name: str, *keys: str) -> int: ...
    def zrem(self, name: str, *values: str) -> int: ...
    def expire(self, name: str, time: int) -> bool: ...


@dataclass(frozen=True)
class CachedMapPinPage:
    page: MapPinPage
    hit: bool


class MapPinCache:
    """Per-profile map pin cache stored in Valkey/Redis.

    Only profiles that open the authenticated map are cached.  Pins are stored in
    a hash keyed by pin PK and ordered by a sorted set scored by that same PK,
    which allows fast keyset pages and targeted updates when one pin changes.
    """

    # Bump whenever MapPinPayloadService.serialize()'s shape changes - the whole
    # point of versioning the key prefix rather than the payload itself is that
    # a stale cache under the old prefix is simply orphaned (and expires via its
    # own TTL) instead of needing an explicit migration.
    VERSION = "v3"
    TTL_SECONDS = 2 * 60 * 60
    LOCK_SECONDS = 30

    def __init__(self, profile: Profile, client: _SyncRedis | None = None):
        self.profile = profile
        self.profile_id = profile.pk
        self.client: _SyncRedis | None = client if client is not None else self._make_client()
        self.payload = MapPinPayloadService(profile)

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL"))

    @classmethod
    def _make_client(cls) -> _SyncRedis | None:
        url = os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL")
        if not url:
            return None
        # redis-py stubs don't distinguish sync vs async return types, so redis.Redis
        # doesn't structurally satisfy _SyncRedis at the type level even though it does
        # at runtime.  This is the single boundary where we assert that fact.
        return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)  # type: ignore[return-value]

    @property
    def _prefix(self) -> str:
        return f"ul:map-pins:{self.VERSION}:profile:{self.profile_id}"

    @property
    def meta_key(self) -> str:
        return f"{self._prefix}:meta"

    @property
    def pins_key(self) -> str:
        return f"{self._prefix}:pins"

    @property
    def order_key(self) -> str:
        return f"{self._prefix}:order"

    @property
    def lock_key(self) -> str:
        return f"{self._prefix}:lock"

    @property
    def rebuild_queued_key(self) -> str:
        return f"{self._prefix}:rebuild-queued"

    def get_or_build_page(self, query: QuerySet[Pin], *, cursor: int | None, limit: int | None, include_total: bool, cacheable: bool = True) -> CachedMapPinPage:
        """The requested page, from cache when this cache can answer it.

        Args:
            query: Pins to serialize on a miss.
            cursor: Exclusive lower bound on pin pk, from a previous page.
            limit: Page size, clamped by the payload service.
            include_total: Also report how many rows match.
            cacheable: Whether *query* is this profile's whole root-pin set.
                What is stored is that one set, keyed by profile and ordered by
                pk with no other predicate, so a narrowed query - a bounding
                box, a filter - is not a question this cache holds the answer
                to and is computed directly instead of being answered wrongly.

        Returns:
            The page, and whether it came from the cache.
        """
        if not self.client or not cacheable:
            return CachedMapPinPage(self.payload.page(query, cursor=cursor, limit=limit, include_total=include_total), hit=False)
        try:
            page = self.get_page(cursor=cursor, limit=limit, include_total=include_total)
            if page is not None:
                return CachedMapPinPage(page, hit=True)
            self.enqueue_rebuild()
        except RedisError:
            logger.warning("Map pin cache unavailable for profile %s", self.profile_id, exc_info=True)
        return CachedMapPinPage(self.payload.page(query, cursor=cursor, limit=limit, include_total=include_total), hit=False)

    def enqueue_rebuild(self) -> None:
        """Queue a full cache rebuild once when the cached page is missing."""
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import rebuild_map_pin_cache

        if not self.client or not self.profile_id:
            return
        try:
            if not self.client.set(self.rebuild_queued_key, "1", nx=True, ex=self.LOCK_SECONDS):
                return
            result = safely_enqueue_task(rebuild_map_pin_cache, self.profile_id)
            if result is None:
                with contextlib.suppress(RedisError):
                    self.client.delete(self.rebuild_queued_key)
        except RedisError:
            logger.warning("Unable to enqueue map pin cache rebuild for profile %s", self.profile_id, exc_info=True)
            with contextlib.suppress(RedisError):
                self.client.delete(self.rebuild_queued_key)

    def get_page(self, *, cursor: int | None, limit: int | None, include_total: bool) -> MapPinPage | None:
        """One page from the cache, or None when the cache cannot be trusted.

        Args:
            cursor: Exclusive lower bound on pin pk, from a previous page.
            limit: Page size, clamped by the payload service.
            include_total: Also report how many pins are cached.

        Returns:
            The page, or None to say "ask the database" - absent, incomplete, or
            self-inconsistent all answer None rather than a partial page.
        """
        if not self.client or not self._is_intact():
            return None
        limit = min(max(int(limit or self.payload.DEFAULT_LIMIT), 1), self.payload.MAX_LIMIT)
        min_score: str | int = f"({cursor}" if cursor else "-inf"
        ids = self.client.zrangebyscore(self.order_key, min_score, "+inf", start=0, num=limit + 1)
        has_more = len(ids) > limit
        ids = ids[:limit]
        raw = self.client.hmget(self.pins_key, ids) if ids else []
        stored = [item for item in raw if item is not None]
        if len(stored) != len(raw):
            # The order index lists pins the payload hash no longer holds, so
            # this page would silently be short. Drop the cache and rebuild
            # rather than return part of an answer.
            self._discard()
            return None
        pins = [json.loads(item) for item in stored]
        next_cursor = int(ids[-1]) if has_more and ids else None
        total = self.client.zcard(self.order_key) if include_total else None
        self._touch()
        return MapPinPage(pins=pins, next_cursor=next_cursor, total=total)

    def _is_intact(self) -> bool:
        """Whether the marker still describes data that is actually present.

        The marker, the payloads and the order index are three keys under one
        eviction policy, on an instance shared with sessions, Channels and the
        Celery broker - and the marker is much the smallest, so it routinely
        outlives what it vouches for. Reading it alone reported a profile with
        no pins as a cache hit, and ``_touch`` then renewed it on the way out,
        so the empty map persisted until something wrote the key again.

        Returns:
            True when the cache can be read. A profile with genuinely no pins
            is intact with no data keys at all, which is why the recorded total
            decides rather than the keys' presence.
        """
        if not self.client or not self.client.exists(self.meta_key):
            return False
        recorded = self.client.hget(self.meta_key, "total")
        try:
            total = int(recorded) if recorded is not None else -1
        except (TypeError, ValueError):
            total = -1
        if total < 0:
            self._discard()
            return False
        if total == 0:
            return True
        if self.client.exists(self.pins_key, self.order_key) != 2 or self.client.zcard(self.order_key) != total:
            self._discard()
            return False
        return True

    def _discard(self) -> None:
        """Drop a cache that cannot be trusted, so the next read rebuilds it."""
        with contextlib.suppress(RedisError):
            self.clear()

    def rebuild(self, query: QuerySet[Pin]) -> None:
        if not self.client:
            return
        lock_token = str(time.time())
        got_lock = self.client.set(self.lock_key, lock_token, nx=True, ex=self.LOCK_SECONDS)
        if not got_lock:
            return
        tmp_pins = f"{self.pins_key}:tmp:{lock_token}"
        tmp_order = f"{self.order_key}:tmp:{lock_token}"
        try:
            pipe = self.client.pipeline(transaction=False)
            count = 0
            for pin in self.payload.all(query):
                pin_id = int(pin["id"])
                pipe.hset(tmp_pins, str(pin_id), json.dumps(pin, separators=(",", ":")))
                pipe.zadd(tmp_order, {str(pin_id): pin_id})
                count += 1
                if count % 500 == 0:
                    pipe.execute()
            pipe.execute()
            if count:
                self.client.rename(tmp_pins, self.pins_key)
                self.client.rename(tmp_order, self.order_key)
            else:
                self.client.delete(self.pins_key, self.order_key)
                self.client.hset(tmp_pins, "__empty__", "1")
                self.client.zadd(tmp_order, {"__empty__": 0})
                self.client.delete(tmp_pins, tmp_order)
            self.client.hset(self.meta_key, mapping={"cached_at": int(time.time()), "total": count})
            self._touch()
        finally:
            with self.client.pipeline(transaction=False) as pipe:
                pipe.delete(tmp_pins)
                pipe.delete(tmp_order)
                pipe.delete(self.lock_key)
                pipe.delete(self.rebuild_queued_key)
                pipe.execute()

    def upsert_pin(self, pin: Pin) -> None:
        if not self.client or not pin.profile_id or pin.profile_id != self.profile_id or not self.client.exists(self.meta_key):
            return
        if pin.parent_pin_id:
            self.delete_pin(pin.pk)
            return
        query = Pin.objects.filter(pk=pin.pk).select_related("location__wiki")
        pins = self.payload.all(query)
        if not pins:
            self.delete_pin(pin.pk)
            return
        payload = json.dumps(pins[0], separators=(",", ":"))
        pin_id_str = str(pin.pk)
        with self.client.pipeline(transaction=False) as pipe:
            pipe.hset(self.pins_key, pin_id_str, payload)
            pipe.zadd(self.order_key, {pin_id_str: int(pin.pk)})
            pipe.execute()
        total = self.client.zcard(self.order_key)
        with self.client.pipeline(transaction=False) as pipe:
            pipe.hset(self.meta_key, mapping={"cached_at": int(time.time()), "total": total})
            pipe.expire(self.meta_key, self.TTL_SECONDS)
            pipe.expire(self.pins_key, self.TTL_SECONDS)
            pipe.expire(self.order_key, self.TTL_SECONDS)
            pipe.execute()

    def delete_pin(self, pin_id: int) -> None:
        if not self.client or not self.client.exists(self.meta_key):
            return
        pin_id_str = str(pin_id)
        with self.client.pipeline(transaction=False) as pipe:
            pipe.hdel(self.pins_key, pin_id_str)
            pipe.zrem(self.order_key, pin_id_str)
            pipe.execute()
        total = self.client.zcard(self.order_key)
        with self.client.pipeline(transaction=False) as pipe:
            pipe.hset(self.meta_key, mapping={"cached_at": int(time.time()), "total": total})
            pipe.expire(self.meta_key, self.TTL_SECONDS)
            pipe.expire(self.pins_key, self.TTL_SECONDS)
            pipe.expire(self.order_key, self.TTL_SECONDS)
            pipe.execute()

    def clear(self) -> None:
        if self.client:
            self.client.delete(*self.all_keys())

    def all_keys(self) -> list[str]:
        """Every key this profile's cache occupies.

        Returns:
            The five key names, so a caller dropping several profiles at once
            can batch them into one command.
        """
        return [self.meta_key, self.pins_key, self.order_key, self.lock_key, self.rebuild_queued_key]

    @classmethod
    def clear_for_profiles(cls, profile_ids: Iterable[int], *, client: _SyncRedis | None = None) -> int:
        """Drop the cached pin sets of several profiles, with one connection.

        The alternative - updating each affected pin's cached payload in place -
        costs a Redis round trip, two queries and a fresh client per pin, which
        is how one label edit came to do tens of thousands of round trips inside
        the editing user's own request (P102). Dropping the set instead costs one
        command, and the next reader rebuilds it from the database at around
        37 ms per 1,000 pins (X17). That is the cache being an accelerator rather
        than something the page cannot be served without.

        Args:
            profile_ids: Whose caches to drop. Duplicates and empties are fine.
            client: Connection to use. Defaults to a new one, which is why this
                is a classmethod: the caller usually has no `Profile` instance,
                only ids.

        Returns:
            How many profiles were dropped, or 0 when there is no cache
            configured to drop them from.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        ids = sorted({profile_id for profile_id in profile_ids if profile_id})
        if not ids:
            return 0
        connection = client if client is not None else cls._make_client()
        if connection is None:
            return 0

        keys = [key for profile_id in ids for key in cls(Profile(pk=profile_id), client=connection).all_keys()]
        # Chunked rather than one DELETE of every key: Valkey executes a command
        # on one thread, so a single delete of tens of thousands of keys is a
        # pause every other client on the instance waits through.
        for start in range(0, len(keys), _DELETE_CHUNK):
            with contextlib.suppress(RedisError):
                connection.delete(*keys[start : start + _DELETE_CHUNK])
        return len(ids)

    def _touch(self) -> None:
        if not self.client:
            return
        with self.client.pipeline(transaction=False) as pipe:
            pipe.expire(self.meta_key, self.TTL_SECONDS)
            pipe.expire(self.pins_key, self.TTL_SECONDS)
            pipe.expire(self.order_key, self.TTL_SECONDS)
            pipe.execute()
