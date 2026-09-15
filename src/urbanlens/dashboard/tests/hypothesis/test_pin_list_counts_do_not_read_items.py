"""A page that shows what a list holds must count its rows in the database, not fetch them.

``PinList.pin_count`` falls back to ``len(self.items.all())``. Every page that renders the add-to-list dialog hands
it lists with no ``with_pin_counts()`` annotation, so showing a number costs one model instance per membership row:
profiled on the capacity population's largest account, the three lists on the map page cost 947 ms of the view's
1,420 ms and 6,727 instantiations.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import EndpointScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile

#: Rows a page may fetch for one more pin on a list. A count is computed in the database, so the honest budget is
#: zero; the allowance absorbs the odd row another part of the page reads as the fixture grows.
MAX_ROWS_PER_MEMBERSHIP = 0.2

#: Model instances one more membership may cost, on the same reasoning.
MAX_OBJECTS_PER_MEMBERSHIP = 0.2

_WAIVER = "the dialog prints one line per list, not per membership, so adding memberships cannot grow the response"


class PinListCountScalingTests(EndpointScalingMixin, TestCase):
    """Putting more pins on a list must not make the pages that show its count read more rows."""

    first_batch = 5
    second_batch = 25

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_list = baker.make(PinList, profile=self.profile, name="Weekend")
        self._seeded = 0
        self.pin = baker.make(Pin, profile=self.profile, location=self.place(), name="Subject")

    def place(self) -> Location:
        """A location with real coordinates, so the profile's map centre is computed once and then cached.

        Returns:
            A saved location near the others."""
        self._seeded += 1
        return baker.make(Location, latitude=f"41.{self._seeded:06d}", longitude=f"-71.{self._seeded:06d}")

    def seed_rows(self, count: int) -> None:
        """Put *count* more pins on the list.

        Args:
            count: How many memberships to add.
        """
        for _ in range(count):
            pin = baker.make(Pin, profile=self.profile, location=self.place(), name=f"Member {self._seeded}")
            PinListItem.objects.create(pin_list=self.pin_list, pin=pin, order=self._seeded)
        # Saving a pin clears the profile's cached map centre, and the next map load recomputes it by reading
        # every pin (models/pin/signals.py). Left alone, that per-pin read is what this test would measure.
        Profile.objects.filter(pk=self.profile.pk).update(
            map_center_latitude="41.000001", map_center_longitude="-71.000001"
        )

    def test_the_map_page_counts_a_list_without_reading_it(self) -> None:
        self.assert_endpoint_scaling(
            reverse("map.view"),
            max_rows_fetched_per_row=MAX_ROWS_PER_MEMBERSHIP,
            max_objects_per_row=MAX_OBJECTS_PER_MEMBERSHIP,
            expect_growth=False,
            growth_waiver=_WAIVER,
        )

    def test_the_pin_page_counts_a_list_without_reading_it(self) -> None:
        self.assert_endpoint_scaling(
            reverse("pin.details", kwargs={"pin_slug": self.pin.slug or str(self.pin.uuid)}),
            max_rows_fetched_per_row=MAX_ROWS_PER_MEMBERSHIP,
            max_objects_per_row=MAX_OBJECTS_PER_MEMBERSHIP,
            expect_growth=False,
            growth_waiver=_WAIVER,
        )

    def test_the_count_is_the_number_of_pins_on_the_list(self) -> None:
        """Without this the budgets above could be passing on a count that is simply wrong."""
        self.seed(self.first_batch)
        listed = PinList.objects.for_profile(self.profile).with_pin_counts().get(pk=self.pin_list.pk)
        self.assertEqual(listed.pin_count, self.first_batch)
