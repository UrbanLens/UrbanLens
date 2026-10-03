"""P186: text a client sent never becomes ``Location.official_name``, so it never reaches the wiki's name or URL.

The paired tests prove the request really ran, that the person still sees the name they sent, and that a provider's
name given at creation does become the slug.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils.text import slugify
from model_bakery import baker

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

    def test_the_pin_keeps_the_name_the_client_sent(self) -> None:
        location = self._add_pin()
        pin = Pin.objects.get(profile=self.profile, location=location)
        self.assertEqual(pin.effective_name, TYPED)
        self.assertFalse(pin.name_is_user_provided)

    def test_a_typed_pin_name_still_wins_over_the_marker_title(self) -> None:
        self.client.post(
            reverse("pin.add"),
            {"name": "My Mill", "latitude": "42.21", "longitude": "-73.71", "place_canonical_name": TYPED},
        )
        pin = Pin.objects.get(profile=self.profile, location__latitude="42.21", location__longitude="-73.71")
        self.assertEqual(pin.name, "My Mill")
        self.assertIsNone(pin.location.official_name)

    def test_text_the_client_sent_does_not_become_the_official_name(self) -> None:
        self.assertNotEqual(self._add_pin().official_name, TYPED)

    def test_text_the_client_sent_does_not_name_the_wiki(self) -> None:
        wiki, _created = Wiki.objects.get_or_create_for_location(self._add_pin())
        self.assertNotEqual(wiki.name, TYPED)

    def test_text_the_client_sent_does_not_reach_the_wiki_url(self) -> None:
        location = self._add_pin()
        self.assertNotIn(slugify(TYPED), reverse("location.wiki", args=[location.slug]))


class TripActivityTitleTests(_SignedIn):
    def setUp(self) -> None:
        super().setUp()
        self.trip = Trip.objects.create(name="Weekend", creator=self.profile)
        TripMembership.objects.get_or_create(trip=self.trip, profile=self.profile, defaults={"rsvp": "yes"})

    def _post(self, body: dict[str, str]) -> None:
        response = self.client.post(
            reverse("trips.activities", kwargs={"trip_slug": self.trip.slug}),
            data=json.dumps(body),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def _add_activity(self) -> TripActivity:
        self._post({"title": TYPED, "geocoded_lat": "42.30", "geocoded_lng": "-73.80"})
        return TripActivity.objects.select_related("location").get(trip=self.trip, title=TYPED)

    def test_the_request_creates_the_location(self) -> None:
        self.assertIsNotNone(self._add_activity().location)

    def test_the_activity_title_does_not_become_the_official_name(self) -> None:
        location = self._add_activity().location
        assert location is not None
        self.assertNotEqual(location.official_name, TYPED)

    def test_the_activity_title_does_not_reach_the_wiki_url(self) -> None:
        location = self._add_activity().location
        assert location is not None
        self.assertNotIn(slugify(TYPED), reverse("location.wiki", args=[location.slug]))

    def test_an_untitled_activity_keeps_the_geocoded_name_as_its_title(self) -> None:
        self._post({"geocoded_name": TYPED, "geocoded_lat": "42.31", "geocoded_lng": "-73.81"})

        activity = TripActivity.objects.select_related("location").get(trip=self.trip)
        assert activity.location is not None
        self.assertEqual(activity.title, TYPED)
        self.assertIsNone(activity.location.official_name)
        self.assertEqual(activity.location.slug, str(activity.location.uuid))


class CreationNameSlugTests(TestCase):
    """The mechanism the URL tests rely on, so they cannot pass merely because slugs stopped coming from names."""

    def test_a_location_created_with_a_provider_name_takes_its_slug_from_it(self) -> None:
        location, _created = Location.objects.get_exact_or_create(
            42.4, -73.9, defaults={"official_name": "Old Grain Mill", "official_name_source": "google_places"}
        )
        self.assertEqual(location.slug, "old-grain-mill")

    def test_a_name_of_unknown_origin_keeps_the_uuid_slug(self) -> None:
        location, _created = Location.objects.get_exact_or_create(
            42.45, -73.92, defaults={"official_name": "Old Grain Mill"}
        )
        self.assertEqual(location.slug, str(location.uuid))

    def test_a_location_created_without_a_name_keeps_its_uuid(self) -> None:
        location, _created = Location.objects.get_exact_or_create(42.5, -73.95)
        self.assertEqual(location.slug, str(location.uuid))


class OfficialByContractTests(TestCase):
    """Readers that present ``official_name`` as a provider's ignore one whose origin is unknown, as legacy rows' is."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)

    def _location(self, source: str) -> Location:
        location, _created = Location.objects.get_exact_or_create(
            42.6 + len(source) / 1000, -73.6, defaults={"official_name": TYPED, "official_name_source": source}
        )
        return location

    def test_a_new_wiki_is_named_only_from_a_provider_name(self) -> None:
        unknown, _ = Wiki.objects.get_or_create_for_location(self._location(""))
        provided, _ = Wiki.objects.get_or_create_for_location(self._location("wikipedia"))

        self.assertNotEqual(unknown.name, TYPED)
        self.assertEqual(provided.name, TYPED)

    def test_the_wiki_labels_only_a_provider_name_official(self) -> None:
        unknown = Wiki.objects.create(location=self._location(""), name="Anything")
        provided = Wiki.objects.create(location=self._location("wikipedia"), name="Anything")

        self.assertIsNone(unknown.official_name)
        self.assertEqual(provided.official_name, TYPED)
        self.assertNotIn(TYPED, [value for _label, value in unknown.deduplicated_identity_fields])

    def test_concealment_shows_only_a_provider_name(self) -> None:
        from urbanlens.dashboard.services.wiki.concealment import concealed_field_values

        unknown = Wiki.objects.create(location=self._location(""), name="Anything")
        provided = Wiki.objects.create(location=self._location("wikipedia"), name="Anything")
        viewer = baker.make(User).profile

        self.assertNotEqual(concealed_field_values(unknown, viewer)["name"], TYPED)
        self.assertEqual(concealed_field_values(provided, viewer)["name"], TYPED)
