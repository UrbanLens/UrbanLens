"""Editing one label must not cost work proportional to the pins carrying it.

A label's icon and colour appear on every pin that carries it and has none of its
own (`resolve_icon`/`resolve_color` in `services.map_pins.payload`), so editing a
label really does change what a lot of pins draw. What P102 is about is *where*
that work happens: `refresh_map_pin_cache_for_label_ids` walks every carrying pin
and calls `_refresh_cached_pin` for each, and each of those - after the request's
transaction commits, still inside the request - re-fetches the Profile, re-fetches
the Pin, constructs a fresh `MapPinCache` (whose `__init__` builds a new Redis
client, `services/map_pins/cache.py:100-107`), and rebuilds that pin's payload.

So a user who owns 20,000 pins and recolours a label they use everywhere spends
tens of thousands of round trips inside their own request. On a gevent worker,
where pure-Python work yields to nothing, that is the shape that takes a worker
away from everyone else sharing it - which is the invariant this whole programme
exists to defend: *no action a user takes should impact the availability of the
site for other users*.

**These tests fail today, deliberately.** They are the TDD reproduction for P102,
written before the fix, and carry `xfail(strict=True)` so the suite stays honest
in both directions: red now would just be noise, and a non-strict xfail would let
the fix land without anyone noticing the marker had gone stale. Strict means the
day the fan-out goes, these *fail for passing* - which is the prompt to delete the
markers and let them stand as ordinary regression tests.

D12 removes the mechanism rather than optimising it: the per-pin cache goes, and a
label edit becomes the single `UPDATE` that `controllers/labels.py:843` already
issues alongside it, with the client learning about it through a derived ETag
rather than through rewritten payloads.

Two details that make the reproduction faithful, and that a naive version gets
wrong:

- The receivers do their work in `transaction.on_commit`, and a Django `TestCase`
  never commits, so without `captureOnCommitCallbacks(execute=True)` the fan-out
  simply does not run and the test passes against broken code.
- `MapPinCache` early-returns when it has no client or the profile is not already
  cached, and pytest sets no Valkey URL, so a test that does not inject a fake
  client measures a cache that declines to do anything. That is exactly why this
  cost has never shown up in the suite.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker
import pytest

from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.map_pins import MapPinCache

#: Two sizes far enough apart that per-pin work is unmistakable, and small enough
#: that the broken implementation still finishes the test in reasonable time.
SMALL = 3
LARGE = 12

#: Queries a label edit may run regardless of how many pins carry the label: the
#: label's own UPDATE, the bump of carrying pins' `updated`, and the handful of
#: lookups around them. Generous on purpose - the assertion is about *slope*, and
#: a constant this loose still fails an implementation that works per pin.
MAX_CONSTANT_QUERIES = 15


class _LabelFanoutCase(TestCase):
    """A profile whose pins all carry one label, with a warm fake cache."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.redis = FakeRedis()
        self.label = baker.make(Label, kind="tag", name="Fanout Probe")
        self._seeded = 0

    def _cache(self) -> MapPinCache:
        return MapPinCache(self.profile, client=self.redis)

    def seed_pins(self, count: int) -> list[Pin]:
        """Create *count* pins for this profile, all carrying the probe label."""
        pins = []
        for _ in range(count):
            self._seeded += 1
            # Locations are unique on (latitude, longitude).
            location = baker.make(
                Location,
                latitude=f"41.{self._seeded:06d}",
                longitude=f"-73.{self._seeded:06d}",
                official_name="Fanout Place",
            )
            pin = baker.make(Pin, profile=self.profile, location=location)
            pin.labels.set([self.label])
            pins.append(pin)
        return pins

    def warm_the_cache(self) -> None:
        """Make the profile look cached, so `upsert_pin` does not early-return.

        Without this the whole fan-out is a no-op and the measurement is of
        nothing - see this module's docstring.
        """
        query = Pin.objects.filter(profile=self.profile).root_pins().select_related("location")
        self._cache().rebuild(query)
        self.assertTrue(self.redis.exists(self._cache().meta_key), "the fake cache did not warm")

    def edit_the_label(self) -> tuple[int, int]:
        """Recolour the label, returning (queries, MapPinCache constructions).

        Returns:
            How many statements ran and how many times `MapPinCache.__init__`
            was entered, both counted across the commit callbacks too.
        """
        constructions = 0
        original_init = MapPinCache.__init__

        def counting_init(cache_self: MapPinCache, *args: Any, **kwargs: Any) -> None:
            nonlocal constructions
            constructions += 1
            kwargs.setdefault("client", self.redis)
            original_init(cache_self, *args, **kwargs)

        self.label.color = f"#{self._seeded:06x}"
        with (
            mock.patch.object(MapPinCache, "__init__", counting_init),
            CaptureQueriesContext(connection) as captured,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.label.save(update_fields=["color"])
        return len(captured.captured_queries), constructions


class LabelEditCostIsFlatTests(_LabelFanoutCase):
    """The cost of a label edit must not track how many pins carry the label."""

    @pytest.mark.xfail(strict=True, reason="P102: the label-edit fan-out is not fixed yet; D12 removes it")
    def test_it_does_not_query_per_pin_carrying_the_label(self) -> None:
        self.seed_pins(SMALL)
        self.warm_the_cache()
        small_queries, _ = self.edit_the_label()

        self.seed_pins(LARGE - SMALL)
        self.warm_the_cache()
        large_queries, _ = self.edit_the_label()

        per_pin = (large_queries - small_queries) / (LARGE - SMALL)
        self.assertLessEqual(
            per_pin,
            0.5,
            f"editing one label ran {small_queries} queries against {SMALL} carrying pins and "
            f"{large_queries} against {LARGE} - {per_pin:.1f} extra statements per pin, inside the "
            "user's own request. P102; see this module's docstring.",
        )

    @pytest.mark.xfail(strict=True, reason="P102: the label-edit fan-out is not fixed yet; D12 removes it")
    def test_it_does_not_build_a_redis_client_per_pin(self) -> None:
        """The per-pin cost is not only SQL - each pin gets its own connection.

        `MapPinCache.__init__` calls `redis.Redis.from_url` when it is given no
        client, so the fan-out's real cost includes one client construction per
        carrying pin. Counted separately from queries because a fix that batched
        the SQL but kept the loop would still pay this.
        """
        self.seed_pins(LARGE)
        self.warm_the_cache()

        _, constructions = self.edit_the_label()

        self.assertLessEqual(
            constructions,
            2,
            f"one label edit constructed {constructions} MapPinCache instances for {LARGE} "
            "carrying pins; each one builds its own Redis client.",
        )

    @pytest.mark.xfail(strict=True, reason="P102: the label-edit fan-out is not fixed yet; D12 removes it")
    def test_the_constant_cost_is_small(self) -> None:
        """Flatness is necessary but not sufficient - the constant matters too."""
        self.seed_pins(SMALL)
        self.warm_the_cache()

        queries, _ = self.edit_the_label()

        self.assertLessEqual(
            queries,
            MAX_CONSTANT_QUERIES,
            f"editing one label ran {queries} queries against only {SMALL} carrying pins",
        )
