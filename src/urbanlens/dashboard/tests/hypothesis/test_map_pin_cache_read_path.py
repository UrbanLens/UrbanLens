"""The map-pin cache must not answer a question it did not cache.

Every authenticated map load goes through `MapPinCache`, and none of its read
path had a test - the only coverage was `enqueue_rebuild` against a `Mock`,
because the cache reads its connection URL from the environment and so opens a
real socket the test network guard refuses. `core.tests.fake_redis` closes that.

Two failures this pins down, both of which return *wrong data* rather than slow
data, and both of which come from the same mismatch: the cache stores the answer
to one fixed question - every root pin for this profile, ordered by pk - but is
consulted for narrower ones, and vouched for by a marker stored in a different
key from the data it describes.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import redis

from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.map_pins import MapPinCache


class MapPinCacheReadPathTests(TestCase):
    """A warm cache must answer exactly what a cold one answers."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        self.redis = FakeRedis()
        # Two clusters far apart, so a bounding box over one excludes the other.
        self.northern = self._pin("44.0", "-73.0", "Northern")
        self.southern = self._pin("30.0", "-97.0", "Southern")

    def _pin(self, latitude: str, longitude: str, name: str) -> Pin:
        location = baker.make(Location, latitude=latitude, longitude=longitude, official_name=name)
        return baker.make(Pin, profile=self.profile, location=location, name=name)

    def _warm_the_cache(self) -> None:
        cache = MapPinCache(self.profile, client=self.redis)
        cache.rebuild(Pin.objects.filter(profile=self.profile).root_pins())
        self.assertTrue(self.redis.exists(cache.meta_key), "the rebuild did not publish a cache to read from")

    def _get(self, **params: Any) -> dict[str, Any]:
        with mock.patch.object(MapPinCache, "_make_client", return_value=self.redis):
            response = self.client.get(reverse("map.pins"), params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_bounding_box_is_honoured_on_a_cache_hit(self) -> None:
        """The cached set is every pin; a narrowed request must not be answered from it."""
        cold = self._get(bbox="43.0,-74.0,45.0,-72.0")
        self.assertEqual([pin["name"] for pin in cold["pins"]], ["Northern"])

        self._warm_the_cache()
        warm = self._get(bbox="43.0,-74.0,45.0,-72.0")

        self.assertEqual(
            [pin["name"] for pin in warm["pins"]],
            ["Northern"],
            "a warm cache returned pins outside the requested bounding box",
        )

    def test_an_unfiltered_request_is_still_served_from_the_cache(self) -> None:
        """The narrowing guard must not cost every ordinary request its cache."""
        self._warm_the_cache()

        payload = self._get()

        self.assertEqual(payload["cache"], "hit")
        self.assertEqual(sorted(pin["name"] for pin in payload["pins"]), ["Northern", "Southern"])

    def test_a_half_evicted_cache_is_a_miss_not_an_empty_map(self) -> None:
        """The marker outliving its data must not read as "this profile has no pins"."""
        self._warm_the_cache()
        cache = MapPinCache(self.profile, client=self.redis)
        # Valkey runs one instance for the cache, sessions, Channels and the
        # Celery broker under a volatile-lru policy, so evicting the payload
        # while its meta key survives is ordinary operation, not a crash.
        self.redis.evict(cache.pins_key, cache.order_key)

        payload = self._get()

        self.assertEqual(
            sorted(pin["name"] for pin in payload["pins"]),
            ["Northern", "Southern"],
            "a half-evicted cache served an empty map as though it were the answer",
        )

    def test_a_half_evicted_cache_recovers_rather_than_renewing_itself(self) -> None:
        """`_touch` must not keep renewing a marker whose data is gone."""
        self._warm_the_cache()
        cache = MapPinCache(self.profile, client=self.redis)
        self.redis.evict(cache.pins_key, cache.order_key)

        self._get()

        self.assertFalse(
            self.redis.exists(cache.meta_key) and not self.redis.exists(cache.pins_key),
            "the stale marker survived the request that found it stale, so every later request repeats the failure",
        )


class PinWritesSurviveADegradedCacheTests(TestCase):
    """Valkey being unreachable or full must not fail the write it was mirroring."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_saving_a_pin_survives_a_refused_connection(self) -> None:
        """`redis.ConnectionError` is not the builtin one, so catching that missed it.

        Nothing in `redis.exceptions` subclasses `OSError` or the builtin
        `ConnectionError`, and the cache mirror runs in an `on_commit` callback -
        so a Valkey that is down, failing over, or out of memory under
        `volatile-lru` raised straight through the request that saved the pin.
        """
        location = baker.make(Location, latitude="45.0", longitude="-70.0")
        pin = baker.make(Pin, profile=self.profile, location=location, name="Before")

        with (
            mock.patch.object(
                MapPinCache, "upsert_pin", side_effect=redis.exceptions.ConnectionError("valkey is down")
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            pin.name = "After"
            pin.save(update_fields=["name"])

        pin.refresh_from_db()
        self.assertEqual(pin.name, "After")

    def test_deleting_a_pin_survives_an_out_of_memory_cache(self) -> None:
        location = baker.make(Location, latitude="46.0", longitude="-70.0")
        pin = baker.make(Pin, profile=self.profile, location=location)
        pin_id = pin.pk

        with (
            mock.patch.object(MapPinCache, "delete_pin", side_effect=redis.exceptions.ResponseError("OOM")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            pin.delete()

        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
