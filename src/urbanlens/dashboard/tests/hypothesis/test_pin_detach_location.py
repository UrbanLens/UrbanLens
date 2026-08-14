"""Detaching a pin from its shared Location currently 500s, and nothing executed the path.

`PinRelinkView` serves two routes. `pin.link.to` (relink to a named Location) is covered by
`test_pin_relink_access.py` and `test_pin_location_conflict.py`. `pin.link` - detach, no
location slug - had no test at all: searching the tree for it returns only `pin.link.delete`,
an unrelated endpoint for removing external links.

The branch builds a new `Location` at the pin's *current* coordinates, and `Location` is
`unique_together = ("latitude", "longitude")`, so it is a guaranteed constraint violation.
See the 2026-08-13 entry in `docs/PROBLEMS.md`.

**Marked `xfail(strict=True)` deliberately.** The correct behaviour is an open product
question - nudge the coordinates, express separation via the pin's own marker fields, or
refuse coherently - and this test must not presuppose the answer. What strict xfail buys is
that the moment detach stops raising, this fails loudly and whoever fixed it is told to
replace the marker with a real assertion. A plain skip would stay silent forever; asserting
the 500 would cement the bug as intended behaviour.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class PinDetachLocationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude=42.1234, longitude=-73.5678)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    @pytest.mark.xfail(strict=True, reason="Detach creates a Location at coordinates one already occupies; see PROBLEMS.md 2026-08-13")
    def test_detaching_a_pin_from_its_location_does_not_error(self) -> None:
        response = self.client.post(reverse("pin.link", args=[self.pin.slug]))

        self.assertLess(response.status_code, 500, "detach raised instead of handling the coordinate collision")

    def test_the_detach_route_exists_and_is_reachable(self) -> None:
        """Guards the test above: if the route name changes, the xfail must not silently 'pass'.

        A strict xfail that errors during *setup* (a NoReverseMatch, say) still counts as an
        expected failure, so the route has to be exercised somewhere that reports honestly.
        """
        url = reverse("pin.link", args=[self.pin.slug])

        self.assertTrue(url)
        self.assertIn(self.pin.slug, url)
