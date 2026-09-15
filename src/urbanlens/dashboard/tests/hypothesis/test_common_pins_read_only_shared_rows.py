"""Which places two people share is read from the pins they share, not from both of their pin lists.

The profile page and its "pins in common" list answered by reading every place each account had pinned, and every
location each had visited, and intersecting them in Python - rows in proportion to how much either person owns,
whoever was looking.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import _row_counting_wrapper
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.common_pins import common_pin_location_ids

MORE_PINS = 10


class CommonPinsReadOnlySharedRowsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.mine = baker.make(User).profile
        self.theirs = baker.make(User).profile
        Profile.objects.filter(pk__in=(self.mine.pk, self.theirs.pk)).update(
            common_pins_visibility=VisibilityChoice.ANYONE
        )
        self.placed = 0
        self.shared = Location.objects.create(latitude=30.0, longitude=40.0)
        for profile in (self.mine, self.theirs):
            baker.make(Pin, profile=profile, location=self.shared, last_visited=timezone.now())

    def _pin(self, profile: Profile, count: int) -> None:
        for _ in range(count):
            self.placed += 1
            location = Location.objects.create(latitude=50 + self.placed * 0.01, longitude=60 + self.placed * 0.01)
            baker.make(Pin, profile=profile, location=location, last_visited=timezone.now())

    def _rows(self, action) -> tuple[object, int]:
        totals = [0]
        with connection.execute_wrapper(_row_counting_wrapper(totals)):
            result = action()
        return result, totals[0]

    def _common_ids(self) -> set[int]:
        return common_pin_location_ids([Profile.objects.get(pk=self.mine.pk), Profile.objects.get(pk=self.theirs.pk)])

    def _profile_page(self) -> dict:
        self.client.force_login(self.mine.user)
        response = self.client.get(reverse("profile.view_user", args=[self.theirs.slug]))
        self.assertEqual(response.status_code, 200)
        return {
            "common_pin_count": response.context["common_pin_count"],
            "shared_visited": [location.pk for location in response.context["shared_visited"]],
        }

    def _common_pins_page(self) -> list[int]:
        self.client.force_login(self.mine.user)
        response = self.client.get(reverse("profile.common_pins", args=[self.theirs.slug]))
        self.assertEqual(response.status_code, 200)
        return [pin.location_id for pin in response.context["common_pins"]]

    def assertRowsDoNotGrowWithPinsOf(self, profile: Profile, action, expected: object) -> None:
        action()  # a first page load records the visit; only the second is comparable
        result, baseline = self._rows(action)
        self.assertEqual(result, expected, "the shared place should be found, or nothing was compared")
        self._pin(profile, MORE_PINS)
        result, rows = self._rows(action)
        self.assertEqual(result, expected)
        self.assertEqual(rows, baseline, f"{MORE_PINS} more unshared pins read {rows - baseline} more rows")

    def test_common_location_ids_do_not_read_my_pins(self) -> None:
        self.assertRowsDoNotGrowWithPinsOf(self.mine, self._common_ids, {self.shared.pk})

    def test_common_location_ids_do_not_read_their_pins(self) -> None:
        self.assertRowsDoNotGrowWithPinsOf(self.theirs, self._common_ids, {self.shared.pk})

    def test_profile_page_does_not_read_my_pins(self) -> None:
        expected = {"common_pin_count": 1, "shared_visited": [self.shared.pk]}
        self.assertRowsDoNotGrowWithPinsOf(self.mine, self._profile_page, expected)

    def test_profile_page_does_not_read_their_pins(self) -> None:
        expected = {"common_pin_count": 1, "shared_visited": [self.shared.pk]}
        self.assertRowsDoNotGrowWithPinsOf(self.theirs, self._profile_page, expected)

    def test_common_pins_page_does_not_read_their_pins(self) -> None:
        self.assertRowsDoNotGrowWithPinsOf(self.theirs, self._common_pins_page, [self.shared.pk])

    def test_a_place_only_one_of_them_visited_is_not_shared_visited(self) -> None:
        Pin.objects.filter(profile=self.theirs).update(last_visited=None)

        self.assertEqual(self._profile_page(), {"common_pin_count": 1, "shared_visited": []})
