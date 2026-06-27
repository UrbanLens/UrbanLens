"""Valkey/Redis-backed cache for authenticated users' map pins."""

from __future__ import annotations

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
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class _SyncPipeline(Protocol):
    """Protocol for the subset of Pipeline methods used by MapPinCache."""

    def hset(self, name: str, key: str | None = ..., value: str | None = ..., mapping: dict[str, Any] | None = ...) -> int: ...
    def zadd(self, name: str, mapping: dict[str, Any]) -> int: ...
    def delete(self, *names: str) -> int: ...
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

    VERSION = "v1"
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

    def get_or_build_page(self, query: QuerySet[Pin], *, cursor: int | None, limit: int | None, include_total: bool) -> CachedMapPinPage:
        if not self.client:
            return CachedMapPinPage(self.payload.page(query, cursor=cursor, limit=limit, include_total=include_total), hit=False)
        try:
            if self.client.exists(self.meta_key):
                page = self.get_page(cursor=cursor, limit=limit, include_total=include_total)
                if page is not None:
                    return CachedMapPinPage(page, hit=True)
            self.enqueue_rebuild()
        except RedisError:
            logger.warning("Map pin cache unavailable for profile %s", self.profile_id, exc_info=True)
        return CachedMapPinPage(self.payload.page(query, cursor=cursor, limit=limit, include_total=include_total), hit=False)

    def enqueue_rebuild(self) -> None:
        """Queue a full cache rebuild once when the cached page is missing."""
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import rebuild_map_pin_cache
        if not self.client or not self.profile_id:
            return
        try:
            if not (_queued := self.client.set(self.rebuild_queued_key, "1", nx=True, ex=self.LOCK_SECONDS)):
                return
            
            result = safely_enqueue_task(rebuild_map_pin_cache, self.profile_id)
            if result is None:
                self.client.delete(self.rebuild_queued_key)
        except RedisError:
            logger.warning("Unable to enqueue map pin cache rebuild for profile %s", self.profile_id, exc_info=True)
            self.client.delete(self.rebuild_queued_key)

    def get_page(self, *, cursor: int | None, limit: int | None, include_total: bool) -> MapPinPage | None:
        if not self.client or not self.client.exists(self.meta_key):
            return None
        limit = min(max(int(limit or self.payload.DEFAULT_LIMIT), 1), self.payload.MAX_LIMIT)
        min_score: str | int = f"({cursor}" if cursor else "-inf"
        ids = self.client.zrangebyscore(self.order_key, min_score, "+inf", start=0, num=limit + 1)
        has_more = len(ids) > limit
        ids = ids[:limit]
        raw = self.client.hmget(self.pins_key, ids) if ids else []
        pins = [json.loads(item) for item in raw if item]
        next_cursor = int(ids[-1]) if has_more and ids else None
        total = self.client.zcard(self.order_key) if include_total else None
        self._touch()
        return MapPinPage(pins=pins, next_cursor=next_cursor, total=total)

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
        if pin.parent_pin_id or pin.parent_location_id:
            self.delete_pin(pin.pk)
            return
        query = Pin.objects.filter(pk=pin.pk).select_related("location")
        pins = self.payload.all(query)
        if not pins:
            self.delete_pin(pin.pk)
            return
        payload = json.dumps(pins[0], separators=(",", ":"))
        self.client.hset(self.pins_key, str(pin.pk), payload)
        self.client.zadd(self.order_key, {str(pin.pk): int(pin.pk)})
        self.client.hset(self.meta_key, mapping={"cached_at": int(time.time()), "total": self.client.zcard(self.order_key)})
        self._touch()

    def delete_pin(self, pin_id: int) -> None:
        if not self.client or not self.client.exists(self.meta_key):
            return
        self.client.hdel(self.pins_key, str(pin_id))
        self.client.zrem(self.order_key, str(pin_id))
        self.client.hset(self.meta_key, mapping={"cached_at": int(time.time()), "total": self.client.zcard(self.order_key)})
        self._touch()

    def clear(self) -> None:
        if self.client:
            self.client.delete(self.meta_key, self.pins_key, self.order_key, self.lock_key, self.rebuild_queued_key)

    def _touch(self) -> None:
        if not self.client:
            return
        self.client.expire(self.meta_key, self.TTL_SECONDS)
        self.client.expire(self.pins_key, self.TTL_SECONDS)
        self.client.expire(self.order_key, self.TTL_SECONDS)
