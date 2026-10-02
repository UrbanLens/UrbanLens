"""P186: ``Location.official_name`` is treated as provider data, but two request paths seed it with text the client sent.

Each xfail asserts what the field is relied on to mean; it fails today and passes once the writer is fixed, when
``strict`` turns the pass into a failure so the marker is removed. The paired test proves the request really ran.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki

TYPED = "Lunch with Sam behind the fence"


class _SignedIn(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)


class AddPinCanonicalNameTests(_SignedIn):
    def _add_pin(self) -> Location:
        response = self.client.post(
            reverse("pin.add"), {"name": "", "latitude": "42.20", "longitude": "-73.70", "place_canonical_name": TYPED}
        )
        self.assertEqual(response.status_code, 200)
        return Pin.objects.get(profile=self.profile, location__latitude="42.20", location__longitude="-73.70").location

    def test_the_request_creates_the_location(self) -> None:
        self.assertIsNotNone(self._add_pin().pk)

    @pytest.mark.xfail(strict=True, reason="P186: place_canonical_name is copied from the POST into official_name")
    def test_text_the_client_sent_does_not_become_the_official_name(self) -> None:
        self.assertNotEqual(self._add_pin().official_name, TYPED)

    @pytest.mark.xfail(strict=True, reason="P186: a wiki adopts official_name as its public name")
    def test_text_the_client_sent_does_not_name_the_wiki(self) -> None:
        wiki, _created = Wiki.objects.get_or_create_for_location(self._add_pin())
        self.assertNotEqual(wiki.name, TYPED)


class TripActivityTitleTests(_SignedIn):
    def _add_activity(self) -> TripActivity:
        trip = Trip.objects.create(name="Weekend", creator=self.profile)
        TripMembership.objects.get_or_create(trip=trip, profile=self.profile, defaults={"rsvp": "yes"})
        response = self.client.post(
            reverse("trips.activities", kwargs={"trip_slug": trip.slug}),
            data=json.dumps({"title": TYPED, "geocoded_lat": "42.30", "geocoded_lng": "-73.80"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return TripActivity.objects.select_related("location").get(trip=trip, title=TYPED)

    def test_the_request_creates_the_location(self) -> None:
        self.assertIsNotNone(self._add_activity().location)

    @pytest.mark.xfail(strict=True, reason="P186: the activity title is the official_name fallback for a new Location")
    def test_the_activity_title_does_not_become_the_official_name(self) -> None:
        location = self._add_activity().location
        assert location is not None
        self.assertNotEqual(location.official_name, TYPED)
