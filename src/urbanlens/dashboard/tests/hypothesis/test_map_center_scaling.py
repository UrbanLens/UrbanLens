"""The map page's centre calculation stays linear in the size of the account.

`Profile.compute_map_center` picks the densest cluster of pins, and the obvious
way to do that compares every point with every other one. That is what it did,
on the critical path of `view_map`:

| pins | haversine calls | measured on chiron |
|---|---|---|
| 1,000 | 1,001,000 | ~1s |
| 10,000 | 100,010,000 | ~1.7 min |
| 20,000 | 400,020,000 | ~7 min |

Found by a 20,000-pin load fixture, where a single `GET /dashboard/map/` pinned
one core and served nothing for nine minutes; every other request to that
process waited behind it, which is the availability invariant failing on one
account's ordinary page load (P108). `services.geo.clustering` replaced the
pairwise scan with a spatial histogram plus a fixed number of refinement passes,
so the work per pin is now a constant.

**Counted, not timed.** A wall-clock assertion on a shared host is a flaky test
that gets deleted; the number of pairwise comparisons is exact, machine
independent, and is the actual property worth holding.

The guards below are what distinguish "the calculation is cheap" from "the test
stopped reaching the calculation" - a reproduction that silently stopped calling
the code under test would look exactly like a fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.integration_testing.perf_seed import (
    ORIGIN_LATITUDE,
    ORIGIN_LONGITUDE,
    seed_heavy_account,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Small, because the cost is quadratic and this runs in the ordinary suite.
#: 40 and 80 are far enough apart to tell linear from quadratic growth (a ratio
#: of 2 against 4) and cheap enough to be unnoticeable.
SMALL = 40
LARGE = SMALL * 2

#: Calls per pin a linear implementation is allowed. Well above 1, because
#: nothing here is arguing about constants — a fix that costs three passes over
#: the points is still a fix, and one that costs `n` per point is not.
MAX_CALLS_PER_PIN = 4.0

#: Growth ratio permitted when the account doubles. Linear is 2.0; quadratic is
#: 4.0. Three sits between them and is not close to either.
MAX_GROWTH_RATIO = 3.0


def count_haversine_calls(profile: Profile) -> int:
    """Run the centre calculation and report how many pairwise distances it took.

    Patches the function `Profile._haversine_km` imports rather than the wrapper
    itself: the import happens inside the function body, so the module attribute
    is what is resolved on each call.

    Args:
        profile: The account whose centre to compute.

    Returns:
        Number of great-circle calculations performed.
    """
    from urbanlens.dashboard.services.geo import distance

    calls = 0
    real = distance.haversine_km

    def counting(*args: float) -> float:
        nonlocal calls
        calls += 1
        return real(*args)

    with mock.patch.object(distance, "haversine_km", counting):
        profile.compute_map_center()
    return calls


class TheMapCentreDoesNotComparePairsTests(TestCase):
    """The cost per pin, asserted on call counts."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile: Profile = baker.make(User).profile

    def test_it_does_not_compare_every_pin_with_every_other_one(self) -> None:
        seed_heavy_account(self.profile, pins=SMALL, analyze=False)

        calls = count_haversine_calls(self.profile)

        self.assertLessEqual(
            calls / SMALL,
            MAX_CALLS_PER_PIN,
            f"{calls} great-circle calculations for {SMALL} pins is {calls / SMALL:.1f} per pin, which is a pairwise scan",
        )

    def test_doubling_the_account_does_not_quadruple_the_work(self) -> None:
        seed_heavy_account(self.profile, pins=SMALL, analyze=False)
        small = count_haversine_calls(self.profile)

        seed_heavy_account(self.profile, pins=LARGE, analyze=False)
        large = count_haversine_calls(self.profile)

        self.assertLessEqual(
            large / small,
            MAX_GROWTH_RATIO,
            f"{small} calls at {SMALL} pins became {large} at {LARGE} - a factor of {large / small:.1f}, where linear is 2.0",
        )


class TheMeasurementIsRealTests(TestCase):
    """Guards.

    Each of these would pass just as happily against a test that had stopped
    calling the code under test, which is the way a scaling assertion goes
    quietly wrong.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile: Profile = baker.make(User).profile

    def test_the_counter_sees_the_real_call_path(self) -> None:
        """If this reaches zero, the counts asserted above are asserting nothing."""
        seed_heavy_account(self.profile, pins=SMALL, analyze=False)

        self.assertGreater(
            count_haversine_calls(self.profile),
            0,
            "the patched function was never called, so the reproductions measure nothing",
        )

    def test_the_centre_lands_inside_the_seeded_block(self) -> None:
        """The calculation has to actually work, whatever it costs."""
        seed_heavy_account(self.profile, pins=SMALL, analyze=False)

        centre = self.profile.compute_map_center()

        self.assertIsNotNone(centre)
        latitude, longitude = centre
        self.assertAlmostEqual(latitude, ORIGIN_LATITUDE, delta=1.0)
        self.assertAlmostEqual(longitude, ORIGIN_LONGITUDE, delta=1.0)

    def test_the_result_is_stored_so_it_is_paid_once(self) -> None:
        """Cached afterwards — which bounds how often it happens, not how long it takes.

        Worth pinning: it is the reason this is survivable at all, and a fix that
        removed the write would turn a one-off stall into one per page load.
        """
        seed_heavy_account(self.profile, pins=SMALL, analyze=False)

        self.profile.compute_map_center()

        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)
        self.assertIsNotNone(self.profile.map_center_longitude)
