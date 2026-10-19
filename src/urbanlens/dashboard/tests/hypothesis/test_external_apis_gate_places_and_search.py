"""The Places layer, place details and the pin web search honour Profile.external_apis_enabled."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription


class _OptedOutCase(TestCase):
    features = f"{SiteFeature.PLACES.value},{SiteFeature.SEARCH.value}"

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.profile.external_apis_enabled = False
        self.profile.save()
        grant_subscription(self.user, baker.make(SubscriptionRole, features=self.features), self.user, None)
        self.client.force_login(self.user)


class PlacesLayerOptOutTests(_OptedOutCase):
    def test_nearby_places_makes_no_upstream_call_and_says_disabled(self) -> None:
        with mock.patch("urbanlens.dashboard.services.map.nearby_places.find_nearby_places") as find:
            response = self.client.get(reverse("map.places.nearby"), {"lat": "40.7", "lng": "-74.0"})
        find.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"places": [], "cached": False, "incomplete": [], "disabled": True})

    def test_nearby_places_still_runs_when_enabled(self) -> None:
        self.profile.external_apis_enabled = True
        self.profile.save()
        found = mock.Mock(places=[], cached=False, incomplete=[])
        with mock.patch(
            "urbanlens.dashboard.services.map.nearby_places.find_nearby_places", return_value=found
        ) as find:
            response = self.client.get(reverse("map.places.nearby"), {"lat": "40.7", "lng": "-74.0"})
        find.assert_called_once()
        self.assertNotIn("disabled", response.json())

    def test_place_details_makes_no_upstream_call_and_refuses(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.places_resolution.get_place_details_full"
        ) as details:
            response = self.client.get(reverse("map.places.details"), {"place_id": "abc"})
        details.assert_not_called()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "disabled"})


class PinWebSearchOptOutTests(_OptedOutCase):
    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(
            Location, official_name="Old Mill Factory", latitude=39.1, longitude=-84.5, city="Cincinnati", state="Ohio"
        )
        self.pin = baker.make(Pin, location=location, profile=self.profile, name="Old Mill Factory")

    def test_an_uncached_search_makes_no_upstream_call_and_refuses(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.pin.search_web") as search:
            response = self.client.get(reverse("pin.web_search", args=[self.pin.slug]))
        search.assert_not_called()
        self.assertEqual(response.status_code, 403)
        self.assertIn("turned off", response.content.decode())

    def test_the_search_still_runs_when_enabled(self) -> None:
        self.profile.external_apis_enabled = True
        self.profile.save()
        with mock.patch("urbanlens.dashboard.controllers.pin.search_web", return_value=[]) as search:
            self.client.get(reverse("pin.web_search", args=[self.pin.slug]))
        search.assert_called()
