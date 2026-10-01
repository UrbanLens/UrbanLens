"""Adding a pin must not make the next page load re-read the whole account.

Creating a pin clears the cached centroid, and the next view of any page that renders a map centre recomputes it
by reading every pin the account owns and clustering them in Python. Measured on the performance population that
is 285 ms for a 20,301-pin account, paid by whichever visitor happens to load next - and paid again after every
single pin, because nothing in production bulk-creates them.

The centre is a map's opening position. One pin out of twenty thousand does not move it, so the recompute does
not have to happen before the page is served.
"""

from __future__ import annotations

import decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_MAP_URL = "/dashboard/map/"

#: A centre already on record, far from the pins below so a recompute is obvious in the value.
_WARM_LAT = decimal.Decimal("42.650000")
_WARM_LNG = decimal.Decimal("-73.750000")


def _pin_at(profile: Profile, lat: float, lng: float) -> Pin:
    """Create a pin whose linked location sits at the given coordinates.

    Args:
        profile: The owning account.
        lat: Latitude for the pin's location.
        lng: Longitude for the pin's location.

    Returns:
        The created pin."""
    return baker.make(Pin, profile=profile, location=baker.make(Location, latitude=lat, longitude=lng))


def _warm_the_centre(profile: Profile) -> None:
    """Put a centre on record, as a recompute would have.

    Args:
        profile: The account to give a cached centre."""
    Profile.objects.filter(pk=profile.pk).update(map_center_latitude=_WARM_LAT, map_center_longitude=_WARM_LNG)


class ANewPinDoesNotCostTheNextVisitorTests(TestCase):
    """The account already has a centre; adding a pin must not throw it away."""

    user: User
    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        for offset in range(3):
            _pin_at(self.profile, 40.0 + offset / 100, -74.0 - offset / 100)
        _warm_the_centre(self.profile)

    def test_adding_a_pin_leaves_the_cached_centre_in_place(self) -> None:
        _pin_at(self.profile, 40.5, -74.5)

        self.profile.refresh_from_db()

        self.assertEqual(self.profile.map_center_latitude, _WARM_LAT)
        self.assertEqual(self.profile.map_center_longitude, _WARM_LNG)

    def test_the_next_map_load_does_not_recompute_the_centre(self) -> None:
        _pin_at(self.profile, 40.5, -74.5)

        with patch.object(Profile, "compute_map_center", return_value=None) as compute:
            response = self.client.get(_MAP_URL)

        self.assertEqual(response.status_code, 200)
        compute.assert_not_called()


class TheInlineComputeStillHappensWhenThereIsNothingToServeTests(TestCase):
    """Guard.

    The fix is "serve what we already have", so it must not turn into "serve nothing". An account with no centre
    on record has to compute one, and at that size it is cheap - this is the first-pin path."""

    user: User
    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_an_account_with_no_centre_yet_computes_one(self) -> None:
        _pin_at(self.profile, 40.0, -74.0)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_latitude)

        centre = self.profile.get_map_center_template_context()

        self.assertAlmostEqual(float(centre["gps_fallback_lat"]), 40.0, places=2)
        self.assertAlmostEqual(float(centre["gps_fallback_lng"]), -74.0, places=2)
