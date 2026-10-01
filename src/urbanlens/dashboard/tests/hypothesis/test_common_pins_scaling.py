"""Viewing a profile must not cost a query per place the two accounts share.

``common_pin_location_ids`` picks one representative ``Location`` per shared
place, and picked it with a ``.first()`` per place - so the profile page ran a
query per thing the two people had both pinned (N21 H30). The page also ran the
whole computation, plus two full-account visit scans, *before* checking whether
the viewer is allowed to see the result, and the setting that decides defaults
to friends-only - so the common case was paying for an answer it then threw
away.
"""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.common_pins import common_pin_location_ids


class _TwoAccountsCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.mine = Profile.objects.get(user=baker.make("auth.User"))
        self.theirs = Profile.objects.get(user=baker.make("auth.User"))

    def _share_places(self, count: int) -> list[Place]:
        """Give both profiles a pin at *count* distinct places, at different coordinates."""
        places = []
        for _ in range(count):
            place = baker.make(Place)
            for profile in (self.mine, self.theirs):
                baker.make(Pin, profile=profile, location=baker.make(Location, place=place))
            places.append(place)
        return places


class OneQueryRegardlessOfHowManyPlacesAreSharedTests(_TwoAccountsCase):
    def test_the_query_count_does_not_grow_with_the_number_of_shared_places(self) -> None:
        self._share_places(2)
        with CaptureQueriesContext(connection) as small:
            common_pin_location_ids([self.mine, self.theirs])

        self._share_places(10)
        with CaptureQueriesContext(connection) as large:
            common_pin_location_ids([self.mine, self.theirs])

        self.assertEqual(
            len(large.captured_queries),
            len(small.captured_queries),
            f"{len(small.captured_queries)} queries for 2 shared places and {len(large.captured_queries)} for 12, "
            "so the page costs a query per thing the two accounts have in common",
        )

    def test_every_shared_place_is_still_found(self) -> None:
        """The half that stops the test above passing against a function that stopped looking."""
        places = self._share_places(4)

        found = common_pin_location_ids([self.mine, self.theirs])

        self.assertEqual(len(found), len(places))
        for location_id in found:
            self.assertTrue(Pin.objects.filter(profile=self.mine, location_id=location_id).exists())

    def test_a_place_only_one_of_them_pinned_is_not_shared(self) -> None:
        self._share_places(1)
        baker.make(Pin, profile=self.theirs, location=baker.make(Location, place=baker.make(Place)))

        self.assertEqual(len(common_pin_location_ids([self.mine, self.theirs])), 1)


class TheAnswerIsNotComputedWhenItMayNotBeShownTests(_TwoAccountsCase):
    """The setting defaults to friends-only, so this is the ordinary path."""

    def _view_their_profile(self) -> None:
        self.client.force_login(self.theirs.user)
        response = self.client.get(reverse("profile.view_user", args=[self.mine.slug]))
        self.assertEqual(response.status_code, 200)

    def test_a_refused_viewer_does_not_pay_for_the_computation(self) -> None:
        self._share_places(3)
        Profile.objects.filter(pk__in=(self.mine.pk, self.theirs.pk)).update(
            common_pins_visibility=VisibilityChoice.FRIENDS
        )

        with mock.patch("urbanlens.dashboard.services.pins.common_pins.common_pin_location_ids") as compute:
            self._view_their_profile()

        compute.assert_not_called()

    def test_a_permitted_viewer_still_gets_the_count(self) -> None:
        """The half that stops the test above passing against a page that dropped the feature."""
        self._share_places(3)
        Profile.objects.filter(pk__in=(self.mine.pk, self.theirs.pk)).update(
            common_pins_visibility=VisibilityChoice.ANYONE
        )

        self.client.force_login(self.theirs.user)
        response = self.client.get(reverse("profile.view_user", args=[self.mine.slug]))

        self.assertEqual(response.context["common_pin_count"], 3)
        self.assertTrue(response.context["can_view_common_pins"])
