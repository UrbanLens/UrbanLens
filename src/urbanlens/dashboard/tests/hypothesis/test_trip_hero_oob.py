"""The trip hero swapped in out of band must match the one the page rendered.

The activities panel loads on every visit and carries a fresh hero, so whatever that copy lacks is
missing from the page for good.
"""

from __future__ import annotations

import re

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripMembership

_OOB_HERO = re.compile(r'<section[^>]*id="trip-hero"[^>]*hx-swap-oob="true".*?</section>', re.DOTALL)


class TripHeroOobTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make("auth.User").profile
        self.member = baker.make("auth.User").profile
        self.trip = Trip.objects.create(name="Hero trip", creator=self.owner)
        TripMembership.objects.create(trip=self.trip, profile=self.owner, rsvp="yes")
        TripMembership.objects.create(trip=self.trip, profile=self.member, rsvp="yes")

    def _oob_hero(self, profile) -> str:
        self.client.force_login(profile.user)
        response = self.client.get(reverse("trips.activities", kwargs={"trip_slug": self.trip.slug}))
        self.assertEqual(response.status_code, 200)
        match = _OOB_HERO.search(response.content.decode())
        self.assertIsNotNone(match, "the activities panel no longer carries an out-of-band hero")
        return match.group(0) if match else ""

    def test_a_joined_member_keeps_the_editable_title(self) -> None:
        for profile in (self.owner, self.member):
            with self.subTest(creator=profile == self.owner):
                hero = self._oob_hero(profile)
                self.assertIn("trip-title-editable", hero)
                self.assertIn("trip-description-editable", hero)

    def test_an_invited_member_who_has_not_joined_gets_the_plain_title(self) -> None:
        TripMembership.objects.filter(trip=self.trip, profile=self.member).update(status=TripMembership.STATUS_INVITED)
        hero = self._oob_hero(self.member)
        self.assertNotIn("trip-title-editable", hero)
        self.assertIn("Hero trip", hero)

    def test_the_back_link_reads_as_the_page_renders_it(self) -> None:
        self.client.force_login(self.owner.user)
        page = self.client.get(reverse("trips.detail", kwargs={"trip_slug": self.trip.slug})).content.decode()
        page_hero = re.search(r'<section[^>]*id="trip-hero".*?</section>', page, re.DOTALL)
        self.assertIsNotNone(page_hero)
        back = re.compile(r'class="[^"]*ul-page-hero__back[^"]*".*?</a>', re.DOTALL)
        page_back = back.search(page_hero.group(0) if page_hero else "")
        oob_back = back.search(self._oob_hero(self.owner))
        self.assertIsNotNone(page_back)
        self.assertEqual(page_back.group(0) if page_back else "", oob_back.group(0) if oob_back else None)
