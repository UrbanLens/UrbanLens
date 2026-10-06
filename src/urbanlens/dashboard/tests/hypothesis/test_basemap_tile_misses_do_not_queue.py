"""Two uncached tiles at once do not wait for each other on the database.

On 0.8.0 every vendor tile reserved its call under ``SELECT ... FOR UPDATE`` on the service's one ``ApiRateLimit``
row. A cold viewport is ~30 misses at once, so they took that lock one after another while each held a pooled
connection; production's slow-request log had SQL at 74% of a miss's wall time, climbing through a burst.

Here someone else holds that row lock for the whole test, and the vendor does not answer either request until both
have arrived. A miss that needed the lock could not reach the vendor, and neither request would finish while the
lock is held.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.core.cache import caches
from django.db import connections, transaction
from django.test import TransactionTestCase

from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.apis.locations.basemap_vendor_tiles_gateway import BasemapVendorTilesGateway
from urbanlens.dashboard.services.core import call_tally, provider_health
from urbanlens.dashboard.services.core.counters import reset_local_fallback
from urbanlens.dashboard.services.core.rate_limiter import get_limit_config
from urbanlens.dashboard.tests.hypothesis.vendor_tile_wire import vendor_answer, vendor_wire

_SERVICE = "basemap_vendor_tiles"
#: How long both requests have to reach the vendor together.
_TOGETHER_SECONDS = 10


class SimultaneousMissesTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        for alias in caches:
            caches[alias].clear()
        reset_local_fallback()
        provider_health.forget_snapshot()
        call_tally.forget_limits()
        get_limit_config(_SERVICE)

    def test_two_misses_reach_the_vendor_together_while_the_limit_row_is_locked(self) -> None:
        barrier = threading.Barrier(2, timeout=_TOGETHER_SECONDS)
        results: dict[int, object] = {}

        def answer(*_args: object, **_kwargs: object) -> object:
            barrier.wait()
            return vendor_answer()

        def miss(x: int) -> None:
            try:
                results[x] = BasemapVendorTilesGateway().download_tile("satellite", 6, x, 1)[0]
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                results[x] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=miss, args=(x,), daemon=True) for x in (1, 2)]
        with vendor_wire(answer) as wire:
            with transaction.atomic():
                ApiRateLimit.objects.select_for_update().get(service=_SERVICE)
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=_TOGETHER_SECONDS + 5)
                finished_while_locked = dict(results)
            # Released: a miss that was waiting on the lock can finish now, so the threads end either way.
            for thread in threads:
                thread.join(timeout=60)

        self.assertEqual(finished_while_locked, {1: 200, 2: 200})
        self.assertEqual(wire.call_count, 2)

    def test_the_limit_holds_across_simultaneous_misses(self) -> None:
        """No lock, but no race past the limit either: the counter is one atomic increment per call."""
        ApiRateLimit.objects.filter(service=_SERVICE).update(calls_per_minute=5)
        call_tally.forget_limits()
        results: list[object] = []
        start = threading.Barrier(12, timeout=_TOGETHER_SECONDS)

        def miss(x: int) -> None:
            try:
                start.wait()
                results.append(BasemapVendorTilesGateway().download_tile("satellite", 6, x, 2)[0])
            except Exception as exc:  # noqa: BLE001 - counted below
                results.append(type(exc).__name__)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=miss, args=(x,), daemon=True) for x in range(12)]
        with mock.patch.object(call_tally, "_now", return_value=60 * 29_000_000 + 30), vendor_wire() as wire:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        self.assertEqual(results.count(200), 5, results)
        self.assertEqual(results.count("RateLimitExceededError"), 7, results)
        self.assertEqual(wire.call_count, 5)
