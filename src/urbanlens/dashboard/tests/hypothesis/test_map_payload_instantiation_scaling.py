"""The map payload must not build a model instance per pin it serializes.

Measured in the app container against 10,000 pins: `MapPinPayloadService.all()`
took 5.79s wall and 5.28s of user CPU - 88% CPU-bound, with Postgres accounting
for 0.36s of it - while constructing 63,240 model objects to emit 10,000 flat
dicts. The same payloads built as JSON straight from SQL took 0.36s, so roughly
96% of that was the object graph rather than the data.

`QueryScalingMixin` cannot see it. `.all()` iterates in chunks of 1,000, so
below that size the query count is literally constant, and even at 10,000 the
21 statements it does run are 6% of the wall time. `RenderTimeScalingMixin`
reads a 6x-too-expensive row as a slow machine on a contended host. What does
separate them is the object count, which is exact and does not move with load -
see `core.tests.instantiation_scaling`.

The pins seeded here carry a location, a wiki, a cover photo and three labels
because that is what fans the count out: `select_related` builds a companion per
row and the label prefetch rebuilds a Label per pin-label pair, so a shared
vocabulary of three labels becomes three objects on every pin.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.instantiation_scaling import InstantiationScalingMixin, count_instantiations
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.map_pins import MapPinCache, MapPinPayloadService

#: Bigger than the mixin's defaults: the payload's per-row cost is what is being
#: measured, and ten extra pins make the marginal count unambiguous.
_FIRST_BATCH = 2
_SECOND_BATCH = 10


class MapPayloadInstantiationScalingTests(InstantiationScalingMixin, TestCase):
    """One more pin on the map must not cost a graph of model objects."""

    first_batch = _FIRST_BATCH
    second_batch = _SECOND_BATCH

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        # Locations are unique on (latitude, longitude), so each seeded pin
        # needs its own coordinate rather than a shared constant.
        self._seeded = 0
        # The whole map shares these three, which is the point: a per-pair
        # rebuild turns a three-row vocabulary into three objects per pin.
        self.labels = [
            baker.make(Label, kind="tag", name="Tag"),
            baker.make(Label, kind="category", name="Category"),
            baker.make(Label, kind="status", name="Status"),
        ]

    def seed_rows(self, count: int) -> None:
        for _ in range(count):
            self._seeded += 1
            location = baker.make(
                Location,
                latitude=f"40.{self._seeded:06d}",
                longitude=f"-74.{self._seeded:06d}",
                official_name="Seeded Place",
            )
            baker.make(Wiki, location=location, name="Seeded Wiki")
            pin = baker.make(Pin, profile=self.profile, location=location)
            pin.labels.set(self.labels)
            pin.cover_photo = baker.make(Image, pin=pin, profile=self.profile, media_type=MediaKind.PHOTO)
            pin.save(update_fields=["cover_photo"])

    def test_the_map_pins_endpoint_does_not_build_objects_per_pin(self) -> None:
        # MapPinCache reads UL_VALKEY_URL straight from the environment rather
        # than through settings, so under test it opens a real socket the test
        # network guard refuses. Without a client it serves from the payload
        # service directly, which is the path being measured here.
        with mock.patch.object(MapPinCache, "make_client", return_value=None):
            self.assert_objects_per_row_bounded(reverse("map.pins"))

    def test_the_payload_service_itself_does_not_build_objects_per_pin(self) -> None:
        """The service, measured directly - `all()` is the unbounded caller's path.

        `map.pins` pages at 500, so the endpoint above can only ever show the
        per-row cost. `all()` is what `get_map_data` (the filter POST and
        `map.init`) and `MapPinCache.rebuild` call with no limit at all, so it
        is measured on its own rather than through a URL.
        """
        self.seed_rows(self.second_batch)
        query = Pin.objects.filter(profile=self.profile).root_pins()
        service = MapPinPayloadService(self.profile)

        with count_instantiations() as counted:
            payloads = service.all(query)

        self.assertEqual(len(payloads), self.second_batch)
        per_pin = counted.total / self.second_batch
        self.assertLessEqual(
            per_pin,
            1.0,
            f"MapPinPayloadService.all() built {counted.total} model objects for "
            f"{self.second_batch} pins ({per_pin:.1f} each) to emit {len(payloads)} dicts: {counted.by_model}",
        )
