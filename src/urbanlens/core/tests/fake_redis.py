"""An in-memory stand-in for the Redis commands the map-pin cache issues.

`MapPinCache` reads its connection URL straight from the environment, so under
test it opens a real socket that the network guard refuses - which is why its
read path (`get_page`, `rebuild`, `upsert_pin`, `delete_pin`) had no coverage at
all while every map load in production went through it. The constructor already
accepts an injected client, so this fills that hole rather than the cache
growing a test-only branch.

Deliberately not a Redis emulator. It implements the commands
`services.map_pins.cache._SyncRedis` declares and no others, with the semantics
those calls depend on: `rename` carries the source key's TTL, `set(nx=True)`
returns None when the key exists, `get` returns exactly what `set` stored
(bytes stay bytes, which is what the gzipped map document needs), and `zrangebyscore` understands ``-inf``,
``+inf`` and the ``(score`` exclusive form the keyset pager uses. Anything it
does not implement should fail loudly instead of quietly returning a default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from types import TracebackType

_UNSET = object()


class FakePipeline:
    """Queues commands and applies them on :meth:`execute`, like redis-py's."""

    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if not hasattr(FakeRedis, name):
            raise AttributeError(f"FakeRedis implements no {name!r}")

        def queue(*args: Any, **kwargs: Any) -> FakePipeline:
            self._queued.append((name, args, kwargs))
            return self

        return queue

    def execute(self) -> list[Any]:
        results = [getattr(self._client, name)(*args, **kwargs) for name, args, kwargs in self._queued]
        self._queued.clear()
        return results

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._queued.clear()


class FakeRedis:
    """The subset of redis-py that `MapPinCache` uses, backed by dicts."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    # -- test affordances ---------------------------------------------------
    def evict(self, *names: str) -> None:
        """Drop keys the way an eviction policy would, without touching the rest.

        The failure this exists to reproduce: a cache whose "is it populated"
        marker outlives the data it vouches for.
        """
        for name in names:
            self.hashes.pop(name, None)
            self.zsets.pop(name, None)
            self.strings.pop(name, None)
            self.ttls.pop(name, None)

    def _all_keys(self) -> set[str]:
        return set(self.hashes) | set(self.zsets) | set(self.strings)

    # -- commands -----------------------------------------------------------
    def exists(self, *names: str) -> int:
        known = self._all_keys()
        return sum(1 for name in names if name in known)

    def get(self, name: str) -> str | bytes | None:
        return self.strings.get(name)

    def set(self, name: str, value: str | bytes, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and name in self._all_keys():
            return None
        self.strings[name] = value
        if ex is not None:
            self.ttls[name] = ex
        return True

    def hset(
        self, name: str, key: str | None = None, value: str | None = None, mapping: dict[str, Any] | None = None
    ) -> int:
        bucket = self.hashes.setdefault(name, {})
        written = 0
        if key is not None:
            bucket[key] = str(value)
            written += 1
        for field, item in (mapping or {}).items():
            bucket[field] = str(item)
            written += 1
        return written

    def hget(self, name: str, key: str) -> str | None:
        return self.hashes.get(name, {}).get(key)

    def hmget(self, name: str, keys: list[str]) -> list[str | None]:
        bucket = self.hashes.get(name, {})
        return [bucket.get(key) for key in keys]

    def hdel(self, name: str, *keys: str) -> int:
        bucket = self.hashes.get(name, {})
        return sum(1 for key in keys if bucket.pop(key, _UNSET) is not _UNSET)

    def zadd(self, name: str, mapping: dict[str, Any]) -> int:
        bucket = self.zsets.setdefault(name, {})
        added = sum(1 for member in mapping if member not in bucket)
        bucket.update({member: float(score) for member, score in mapping.items()})
        return added

    def zrem(self, name: str, *values: str) -> int:
        bucket = self.zsets.get(name, {})
        return sum(1 for value in values if bucket.pop(value, _UNSET) is not _UNSET)

    def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    def zrangebyscore(
        self, name: str, min_score: str | int, max_score: str | int, start: int = 0, num: int | None = None
    ) -> list[str]:
        low, low_exclusive = _bound(min_score, float("-inf"))
        high, high_exclusive = _bound(max_score, float("inf"))
        members = sorted(self.zsets.get(name, {}).items(), key=lambda item: (item[1], item[0]))
        selected = [
            member
            for member, score in members
            if (score > low if low_exclusive else score >= low) and (score < high if high_exclusive else score <= high)
        ]
        selected = selected[start:]
        return selected[:num] if num is not None else selected

    def rename(self, src: str, dst: str) -> bool:
        for store in (self.hashes, self.zsets, self.strings):
            if src in store:
                store[dst] = store.pop(src)  # type: ignore[assignment]
                # RENAME carries the source's TTL and discards the destination's.
                ttl = self.ttls.pop(src, None)
                if ttl is None:
                    self.ttls.pop(dst, None)
                else:
                    self.ttls[dst] = ttl
                return True
        raise KeyError(f"no such key: {src}")

    def delete(self, *names: str) -> int:
        removed = sum(1 for name in names if name in self._all_keys())
        self.evict(*names)
        return removed

    def expire(self, name: str, time: int) -> bool:
        if name not in self._all_keys():
            return False
        self.ttls[name] = time
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


def _bound(raw: str | int, default: float) -> tuple[float, bool]:
    """Parse a zrangebyscore bound, honouring the ``(score`` exclusive form."""
    text = str(raw)
    exclusive = text.startswith("(")
    if exclusive:
        text = text[1:]
    if text in {"-inf", "+inf", "inf"}:
        return (float(text.replace("+", "")), exclusive)
    try:
        return (float(text), exclusive)
    except ValueError:
        return (default, exclusive)
