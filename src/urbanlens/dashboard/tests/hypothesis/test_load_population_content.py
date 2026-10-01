"""The capacity population carries the content its journeys render, not only pins.

A pin page with no comments, a trips page with no trips, an Organize page with no customizations and a pin save that
syncs no smart lists all cost less than they do for a real account, so a capacity run over them overstates capacity.
"""

from __future__ import annotations

from django.contrib.auth.models import User

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.integration_testing.population import (
    COMMENTS_PER_PIN,
    LABEL_CUSTOMIZATIONS,
    SMART_LISTS,
    TRIP_ACTIVITIES,
    SizeTier,
    provision_population,
)

TINY = (SizeTier(share=1.0, min_pins=12, max_pins=12),)
ACCOUNTS = 3


def _content_counts() -> tuple[int, ...]:
    return (
        Comment.objects.count(),
        Trip.objects.count(),
        TripMembership.objects.count(),
        TripActivity.objects.count(),
        PinList.objects.count(),
        PinListItem.objects.count(),
        LabelCustomization.objects.count(),
    )


class ThePopulationCarriesContentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for position in range(LABEL_CUSTOMIZATIONS):
            Label.objects.create(name=f"Shared {position}", kind=KIND_TAG)
        self.result = provision_population(ACCOUNTS, tiers=TINY)
        self.profiles = [User.objects.get(username=account.username).profile for account in self.result.accounts]

    def _first_pin(self, profile: Profile) -> Pin:
        pin = Pin.objects.filter(profile=profile).root_pins().select_related("location").order_by("pk").first()
        assert pin is not None  # nosec B101
        return pin

    def test_each_accounts_first_pin_carries_comments(self) -> None:
        for profile in self.profiles:
            with self.subTest(profile=profile.pk):
                self.assertEqual(Comment.objects.filter(pin=self._first_pin(profile)).count(), COMMENTS_PER_PIN)

    def test_each_accounts_wiki_is_discussed_by_more_than_its_owner(self) -> None:
        for profile in self.profiles:
            with self.subTest(profile=profile.pk):
                comments = Comment.objects.filter(wiki=Wiki.objects.get(location=self._first_pin(profile).location))
                self.assertEqual(comments.count(), COMMENTS_PER_PIN)
                self.assertTrue(comments.exclude(profile=profile).exists())

    def test_each_account_plans_a_trip_with_a_friend_through_its_own_pins(self) -> None:
        for profile in self.profiles:
            with self.subTest(profile=profile.pk):
                trip = Trip.objects.get(creator=profile)
                members = set(TripMembership.objects.filter(trip=trip).values_list("profile_id", flat=True))
                self.assertIn(profile.pk, members)
                self.assertTrue(members - {profile.pk}, "a trip nobody else is on renders nothing shared")
                activities = TripActivity.objects.filter(trip=trip)
                self.assertEqual(activities.count(), TRIP_ACTIVITIES)
                self.assertFalse(activities.exclude(pin__profile=profile).exists())

    def test_each_account_has_smart_lists_already_synced(self) -> None:
        for profile in self.profiles:
            with self.subTest(profile=profile.pk):
                smart_lists = PinList.objects.active_smart_lists(profile)
                self.assertEqual(smart_lists.count(), SMART_LISTS)
                pins = Pin.objects.filter(profile=profile).root_pins().count()
                for pin_list in smart_lists:
                    members = PinListItem.objects.filter(pin_list=pin_list).count()
                    self.assertTrue(0 < members < pins, "a list that holds every pin is the map, not a list")

    def test_each_account_restyles_global_labels_its_own_pins_carry(self) -> None:
        for profile in self.profiles:
            with self.subTest(profile=profile.pk):
                customized = LabelCustomization.objects.filter(profile=profile)
                self.assertEqual(customized.count(), LABEL_CUSTOMIZATIONS)
                self.assertFalse(
                    customized.exclude(label__profile__isnull=True).exists(), "own labels are edited directly"
                )
                carried = Pin.labels.through.objects.filter(
                    pin__profile=profile, label_id__in=customized.values("label_id")
                )
                self.assertEqual(carried.values("label_id").distinct().count(), LABEL_CUSTOMIZATIONS)

    def test_a_second_run_adds_no_content(self) -> None:
        before = _content_counts()

        provision_population(ACCOUNTS, tiers=TINY)

        self.assertEqual(_content_counts(), before)
