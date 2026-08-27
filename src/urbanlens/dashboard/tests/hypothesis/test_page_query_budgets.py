"""Query counts for the main pages, measured against a non-trivial dataset.

Written as a measurement harness first and a regression guard second. An N+1 is
invisible on a fixture with one pin: the page issues one extra query, the test
passes, and the same code issues four hundred for a real account. Every check
here therefore builds a *second* dataset roughly twice the size and asserts the
count did not grow with it - which is the property that actually matters and the
one a fixed budget number cannot express.

The absolute budgets sit at roughly 1.7x each page's measured cost, so a genuine
regression trips them while a legitimate extra `select_related` does not. Measured
2026-08-13 at 4 and 24 pins: map 17/17, organize 28/28, profile 15/15, pin detail
26/24. Every one is flat, which is the point - `organize.index` in particular was
244 queries earlier in this audit.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_SMALL = 4
_LARGE = 9


class PageQueryBudgetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self._seed(_SMALL)

    def _seed(self, count: int) -> list[Pin]:
        """Add *count* pins, each with its own location and a couple of labels.

        Coordinates advance from a running offset rather than restarting at zero:
        ``Location`` is unique on (latitude, longitude), so a second seeding pass
        that reused the first pass's grid would collide instead of growing the
        dataset.
        """
        pins = []
        start = getattr(self, "_seeded", 0)
        self._seeded = start + count
        for i in range(start, start + count):
            pin = baker.make(
                Pin,
                profile=self.profile,
                name=f"Pin {i} {baker.random_gen.gen_string(6)}",
                location=baker.make(Location, latitude=41.0 + i / 100, longitude=-71.0 - i / 100),
            )
            for kind in ("tag", "category"):
                pin.labels.add(baker.make(Label, profile=self.profile, kind=kind))
            pins.append(pin)
        return pins

    def _count(self, url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertIn(response.status_code, (200, 302), f"{url} returned {response.status_code}")
        return len(ctx.captured_queries)

    def _assert_flat(self, url: str, *, budget: int) -> None:
        """The count must not grow when the dataset roughly doubles."""
        small = self._count(url)
        self._seed(_LARGE - _SMALL)
        large = self._count(url)

        self.assertLessEqual(
            large,
            small + 2,
            f"{url}: {small} queries at {_SMALL} pins, {large} at {_LARGE} - scales with row count",
        )
        self.assertLessEqual(large, budget, f"{url}: {large} queries exceeds the {budget} budget")

    def test_the_fixture_is_big_enough_to_expose_scaling(self) -> None:
        """Guards every check below from passing on an empty dataset."""
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), _SMALL)
        self.assertGreaterEqual(Label.objects.filter(profile=self.profile).count(), _SMALL * 2)

    def test_map_page(self) -> None:
        self._assert_flat(reverse("map.view"), budget=30)

    def test_pin_detail(self) -> None:
        pin = Pin.objects.filter(profile=self.profile).first()
        assert pin is not None
        self._assert_flat(reverse("pin.details", args=[pin.slug]), budget=45)

    def test_organize_page(self) -> None:
        self._assert_flat(reverse("organize.index"), budget=45)

    def test_profile_page(self) -> None:
        self._assert_flat(reverse("profile.view"), budget=30)
