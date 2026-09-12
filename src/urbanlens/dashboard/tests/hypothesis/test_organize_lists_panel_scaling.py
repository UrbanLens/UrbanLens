"""The Organize lists panel loads every pin on every list to print a count.

N21 H29. `PinListsIndexView` runs `prefetch_related("items__pin")` and the
template uses exactly one thing from it - `pin_list.pin_count`, which is
`len(self.items.all())`. So every pin on every list the profile owns is fetched
and built into a model instance to produce an integer the database can count
without sending a row, and the `pin_count` sort materialises the whole queryset
in Python on top of that.

The axis that matters is pins, not lists: the panel renders one card per list
either way, so a profile with ten lists of five thousand pins pays for fifty
thousand objects to draw ten cards. Stated as "adding pins must not change what
the panel costs", which is the invariant, rather than as a fixed budget that
would need recalibrating whenever a card grows a field.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.instantiation_scaling import count_instantiations
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile

_LISTS = 3


class _PanelCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _seed(self, pins_per_list: int) -> None:
        for _ in range(_LISTS):
            pin_list = baker.make(PinList, profile=self.profile)
            for _ in range(pins_per_list):
                pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))
                baker.make(PinListItem, pin_list=pin_list, pin=pin)

    def _load(self, sort: str = "updated"):
        return self.client.get(
            reverse("lists.list"),
            {"tab": "lists", "sort": sort},
            headers={"hx-request": "true"},
        )


class ThePanelDoesNotPayPerPinTests(_PanelCase):
    """Same number of cards, ten times the pins behind them."""

    def _objects_for(self, pins_per_list: int, sort: str) -> int:
        self._seed(pins_per_list)
        with count_instantiations() as counted:
            response = self._load(sort)
        assert response.status_code == 200, response.status_code  # nosec B101
        return counted.total

    def test_the_default_sort_does_not_build_an_object_per_pin(self) -> None:
        small = self._objects_for(1, "updated")
        PinList.objects.all().delete()
        large = self._objects_for(10, "updated")

        self.assertLessEqual(
            large - small,
            _LISTS,
            f"the panel built {large - small} more objects for 9 more pins per list; it is loading pins to count them",
        )

    def test_the_pin_count_sort_does_not_build_an_object_per_pin(self) -> None:
        """The sort path materialises the queryset in Python, so it needs its own guard."""
        small = self._objects_for(1, "pin_count")
        PinList.objects.all().delete()
        large = self._objects_for(10, "pin_count")

        self.assertLessEqual(
            large - small,
            _LISTS,
            f"the pin_count sort built {large - small} more objects for 9 more pins per list",
        )


class ThePanelStillWorksTests(_PanelCase):
    """The half that stops the budget above passing against a panel that renders nothing."""

    def test_each_list_still_shows_its_real_pin_count(self) -> None:
        self._seed(4)

        body = self._load().content.decode()

        self.assertIn("4 pins", body)

    def test_the_pin_count_sort_still_orders_by_count(self) -> None:
        biggest = baker.make(PinList, profile=self.profile, name="Biggest")
        smallest = baker.make(PinList, profile=self.profile, name="Smallest")
        for _ in range(5):
            baker.make(
                PinListItem, pin_list=biggest, pin=baker.make(Pin, profile=self.profile, location=baker.make(Location))
            )
        baker.make(
            PinListItem, pin_list=smallest, pin=baker.make(Pin, profile=self.profile, location=baker.make(Location))
        )

        body = self._load("pin_count").content.decode()

        self.assertLess(body.index("Biggest"), body.index("Smallest"))
