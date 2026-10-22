"""The Places layer, place details and a pin's web search make no external call for a user who turned External Services off (UL-337)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin import PinController
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

_LANDMARKS = "urbanlens.dashboard.services.apis.locations.places_resolution.search_nearby_landmarks"
_DETAILS = "urbanlens.dashboard.services.apis.locations.places_resolution.get_place_details_full"
_WIKIPEDIA = "urbanlens.dashboard.services.apis.assets.wikipedia.WikipediaGateway"


class _GatedCase(TestCase):
    external_apis_enabled = False

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        profile = self.user.profile
        profile.external_apis_enabled = self.external_apis_enabled
        profile.places_google_enabled = True
        profile.places_wikipedia_enabled = True
        profile.save()
        patcher = mock.patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class PlacesLayerGateTests(_GatedCase):
    def test_the_layer_asks_no_source_and_answers_disabled(self) -> None:
        with mock.patch(_LANDMARKS) as landmarks, mock.patch(_WIKIPEDIA) as wikipedia:
            response = self.client.get(reverse("map.places.nearby"), {"lat": "40.0", "lng": "-74.0", "zoom": "15"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["disabled"])
        self.assertEqual(response.json()["places"], [])
        landmarks.assert_not_called()
        wikipedia.assert_not_called()


class PlaceDetailsGateTests(_GatedCase):
    def test_details_are_not_fetched_and_answer_disabled(self) -> None:
        with mock.patch(_DETAILS) as details:
            response = self.client.get(reverse("map.places.details"), {"place_id": "p1"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "disabled"})
        details.assert_not_called()


class PlacesLayerEnabledTests(_GatedCase):
    external_apis_enabled = True

    def test_the_same_requests_are_answered_when_the_user_allows_external_services(self) -> None:
        with mock.patch(_DETAILS, return_value={"name": "Mill"}) as details:
            response = self.client.get(reverse("map.places.details"), {"place_id": "p1"})

        self.assertEqual(response.status_code, 200)
        details.assert_called_once()


class PinWebSearchGateTests(TestCase):
    def _request(self, *, external_apis_enabled: bool) -> tuple[Pin, HttpRequest]:
        location = baker.make(
            Location,
            official_name="Official Test Location",
            official_name_source="google_places",
            latitude=41.0,
            longitude=-81.5,
        )
        baker.make(User)  # the first user is auto-promoted to site admin
        user = baker.make(User)
        profile = user.profile
        profile.external_apis_enabled = external_apis_enabled
        profile.save()
        pin = baker.make(Pin, location=location, profile=profile)
        grant_subscription(user, baker.make(SubscriptionRole, features=SiteFeature.SEARCH), user, None)
        request = RequestFactory().get("/")
        request.user = user
        return pin, request

    def test_no_live_search_is_made_and_the_card_says_why(self) -> None:
        pin, request = self._request(external_apis_enabled=False)
        with mock.patch("urbanlens.dashboard.controllers.pin.search_web") as search_web:
            response = PinController().web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 403)
        self.assertIn("External services are off.", response.content.decode())
        search_web.assert_not_called()

    def test_a_search_is_made_when_the_user_allows_external_services(self) -> None:
        pin, request = self._request(external_apis_enabled=True)
        with mock.patch("urbanlens.dashboard.controllers.pin.search_web", return_value=[]) as search_web:
            PinController().web_search(request, pin_slug=pin.slug)

        search_web.assert_called()

    def test_a_partly_cached_pin_serves_what_is_cached_without_a_live_search(self) -> None:
        from urbanlens.dashboard.services.search.pin_web_search import pin_web_searches, store_web_search

        pin, request = self._request(external_apis_enabled=False)
        pin.name = "User Edited Location"
        pin.save()
        searches = pin_web_searches(pin)
        self.assertGreater(len(searches), 1, "a renamed pin has its own search besides the shared one")
        store_web_search(
            pin.location, searches[0], [{"title": "Cached hit", "link": "http://example.com/a", "snippet": "s"}]
        )

        with mock.patch("urbanlens.dashboard.controllers.pin.search_web") as search_web:
            response = PinController().web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Cached hit", response.content.decode())
        search_web.assert_not_called()
