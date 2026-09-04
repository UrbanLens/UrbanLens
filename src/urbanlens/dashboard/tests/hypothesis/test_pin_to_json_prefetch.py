"""Does `Pin.to_json()` honour a `prefetch_related("labels")`, or defeat it?

`to_json()` builds its payload with `self.labels.filter(kind=...)` twice. `.filter()` on a
prefetched many-to-many constructs a *new* queryset rather than reading the prefetch cache,
so a caller that prefetches and then serialises N pins can still pay 2N queries.

This is a measurement, not an assertion of a bug: the test records the actual query count
for 1 pin and for 5. If the count scales with the number of pins, the cache is bypassed.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class PinToJsonPrefetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))
        self.status = Label.objects.create(profile=self.profile, name="ZzAudit St", kind=KIND_STATUS)
        self.tag = Label.objects.create(profile=self.profile, name="ZzAudit Tg", kind=KIND_TAG)

    def _make_pins(self, n: int, start: int = 0) -> None:
        # Location is unique_together(latitude, longitude), so batches must not overlap.
        for i in range(start, start + n):
            loc = baker.make(
                Location,
                latitude=42.0 + i / 1000,
                longitude=-73.0 - i / 1000,
                point=Point(-73.0 - i / 1000, 42.0 + i / 1000, srid=4326),
            )
            pin = baker.make(Pin, profile=self.profile, location=loc, name=f"ZzAudit Pin {i}")
            pin.labels.add(self.status, self.tag)

    def _count_for(self, n: int) -> int:
        qs = (
            Pin.objects.filter(profile=self.profile)
            .select_related("location", "profile")
            .prefetch_related("labels", "reviews")
        )
        with CaptureQueriesContext(connection) as ctx:
            [p.to_json() for p in qs]
        return len(ctx)

    def test_labels_are_read_from_the_prefetch_cache(self) -> None:
        """Guards the fix: `to_json()` must not re-query labels per pin.

        Measured before the fix: 1 pin -> 6 queries, 5 pins -> 22, i.e. **4 per pin**
        despite `prefetch_related("labels")`, because `.filter()` on a prefetched m2m
        builds a fresh queryset and ignores the cache. After: 4 and 12, i.e. 2 per pin.

        The remaining 2 per pin are *not* labels - they come from other per-pin lookups
        in the same payload (a rating fetch among them) and are filed separately. This
        asserts the label half only, so it fails if anyone reintroduces `.filter()` here
        and keeps passing when the rest is addressed.
        """
        self._make_pins(1)
        one = self._count_for(1)
        self._make_pins(4, start=1)
        five = self._count_for(5)
        per_pin = (five - one) / 4

        self.assertLessEqual(
            per_pin,
            0,
            f"{per_pin:.1f} queries per pin (1 pin -> {one}, 5 pins -> {five}); a prefetch is being bypassed - labels via .filter(), or reviews via .latest()",
        )
