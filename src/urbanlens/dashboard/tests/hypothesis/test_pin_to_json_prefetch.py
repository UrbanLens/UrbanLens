"""Does `Pin.to_json()` honour a `prefetch_related("labels")`, or defeat it?"""

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

        Measured before the fix: 1 pin -> 6 queries, 5 pins -> 22, i.e. **4 per pin** despite
        `prefetch_related("labels")`, because `.filter()` on a prefetched m2m builds a fresh queryset and
        ignores the cache."""
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
