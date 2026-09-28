"""Place search, place details and the Places layer call their upstreams under the request-path policy.

N29 G5-25, G5-28, G6-1. Before: autocomplete called Google/REData on every keystroke with no cache,
throttle or deadline; a nearby-places miss called three sources one after another (about 70 s worst
case) and cached whatever came back, a failure included, for 90 days; the radius was part of the cache
key, so any integer made a miss.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis import request_upstreams
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.map.nearby_places import grid_cell, radius_bucket
from urbanlens.dashboard.services.security.throttle import Rate
from urbanlens.UrbanLens.settings.app import settings as app_settings

_AUTOCOMPLETE = "urbanlens.dashboard.services.apis.locations.places_resolution.autocomplete_predictions"
_RESOLVE = "urbanlens.dashboard.services.apis.locations.places_resolution.resolve_place_coordinates"
_DETAILS = "urbanlens.dashboard.services.apis.locations.places_resolution.get_place_details_full"
_LANDMARKS = "urbanlens.dashboard.services.apis.locations.places_resolution.search_nearby_landmarks"
_WIKIPEDIA = "urbanlens.dashboard.services.apis.assets.wikipedia.WikipediaGateway"
_PARKS = "urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway.RedataNationalParksGateway"

_UPSTREAMS = [
    request_upstreams.PlacesAutocompleteUpstream,
    request_upstreams.PlaceResolveUpstream,
    request_upstreams.PlaceDetailsUpstream,
    request_upstreams.NearbyLandmarksUpstream,
    request_upstreams.NationalParksUpstream,
    request_upstreams.WikipediaNearbyUpstream,
]


class _PlacesCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for upstream in _UPSTREAMS:
            upstream.reset()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        for patcher in (
            mock.patch.object(app_settings, "google_unrestricted_api_key", "test-key"),
            mock.patch.object(app_settings, "redata_api_url", ""),
            mock.patch.object(app_settings, "redata_api_key", ""),
            mock.patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _hang(self, *_args, **_kwargs):
        self.release.wait(timeout=10)
        return []


class AutocompleteTests(_PlacesCase):
    def _search(self, q: str = "old mill"):
        return self.client.get(reverse("map.autocomplete.places"), {"q": q})

    def test_a_repeated_query_is_answered_from_the_cache(self) -> None:
        prediction = {"place_id": "p1", "main_text": "Old Mill", "secondary_text": "NY"}
        with mock.patch(_AUTOCOMPLETE, return_value=[prediction]) as provider:
            first = self._search("Old  Mill")
            second = self._search("old mill")

        self.assertEqual([r["place_id"] for r in first.json()["results"]], ["p1"])
        self.assertEqual(second.json()["results"], first.json()["results"])
        self.assertEqual(provider.call_count, 1)

    def test_a_failure_is_reported_and_not_cached(self) -> None:
        with mock.patch(_AUTOCOMPLETE, side_effect=[GatewayRequestError("down"), []]) as provider:
            failed = self._search()
            retried = self._search()

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(provider.call_count, 2)

    def test_a_hanging_provider_costs_the_request_only_the_deadline(self) -> None:
        started = time.monotonic()
        with (
            mock.patch.object(request_upstreams.PlacesAutocompleteUpstream, "deadline", 0.2),
            mock.patch(_AUTOCOMPLETE, side_effect=self._hang),
        ):
            response = self._search()

        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response)
        self.assertLess(time.monotonic() - started, 3)

    def test_one_account_is_throttled_on_uncached_queries(self) -> None:
        with (
            mock.patch.object(request_upstreams.PlacesAutocompleteUpstream, "rate", Rate(limit=2, window_seconds=60)),
            mock.patch(_AUTOCOMPLETE, return_value=[]),
        ):
            statuses = [self._search(f"query {i}").status_code for i in range(3)]
            repeat = self._search("query 0")

        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(repeat.status_code, 200, "a cached answer costs nothing against the throttle")


class ResolvePlaceTests(_PlacesCase):
    def test_a_failure_is_an_error_rather_than_a_confident_not_found(self) -> None:
        with mock.patch(_RESOLVE, side_effect=GatewayRequestError("down")):
            response = self.client.get(reverse("map.resolve_place"), {"place_id": "p1"})

        self.assertEqual(response.status_code, 502)

    def test_a_resolved_place_is_cached(self) -> None:
        with mock.patch(_RESOLVE, return_value=(40.0, -74.0, "Old Mill")) as provider:
            first = self.client.get(reverse("map.resolve_place"), {"place_id": "p1"})
            second = self.client.get(reverse("map.resolve_place"), {"place_id": "p1"})

        self.assertEqual(first.json(), {"lat": 40.0, "lng": -74.0, "name": "Old Mill"})
        self.assertEqual(second.json(), first.json())
        self.assertEqual(provider.call_count, 1)


class PlaceDetailsTests(_PlacesCase):
    def test_a_failure_is_a_502_and_is_not_cached(self) -> None:
        with mock.patch(_DETAILS, side_effect=[GatewayRequestError("down"), {"name": "Mill"}]) as provider:
            failed = self.client.get(reverse("map.places.details"), {"place_id": "p1"})
            retried = self.client.get(reverse("map.places.details"), {"place_id": "p1"})
            cached = self.client.get(reverse("map.places.details"), {"place_id": "p1"})

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(retried.json(), {"place": {"name": "Mill"}, "cached": False})
        self.assertEqual(cached.json(), {"place": {"name": "Mill"}, "cached": True})
        self.assertEqual(provider.call_count, 2)


class NearbyPlacesTests(_PlacesCase):
    def setUp(self) -> None:
        super().setUp()
        profile = self.user.profile
        profile.places_google_enabled = True
        profile.places_nps_enabled = False
        profile.places_wikipedia_enabled = True
        profile.save()

    def _nearby(self, **params):
        return self.client.get(
            reverse("map.places.nearby"), {"lat": "40.0012", "lng": "-74.0031", "zoom": "15", **params}
        )

    def _landmark(self, name: str = "Old Mill"):
        return {"id": name, "displayName": {"text": name}, "location": {"latitude": 40.0, "longitude": -74.0}}

    def test_radii_in_the_same_bucket_share_one_answer(self) -> None:
        with mock.patch(_LANDMARKS, return_value=[self._landmark()]) as landmarks, mock.patch(_WIKIPEDIA):
            self._nearby(radius="1500")
            second = self._nearby(radius="1999")

        self.assertEqual(landmarks.call_count, 1)
        self.assertEqual(landmarks.call_args.args[2], 2000)
        self.assertEqual([p["name"] for p in second.json()["places"]], ["Old Mill"])

    def test_any_radius_is_snapped_to_a_known_bucket(self) -> None:
        self.assertEqual(
            [radius_bucket(r) for r in (1, 500, 501, 4999, 5000, 10**9)], [500, 500, 1000, 5000, 5000, 5000]
        )

    def test_sources_are_asked_at_the_cell_centre_the_answer_is_cached_under(self) -> None:
        with mock.patch(_LANDMARKS, return_value=[]) as landmarks, mock.patch(_WIKIPEDIA):
            self._nearby()

        self.assertEqual(landmarks.call_args.args[:2], grid_cell(40.0012, -74.0031))

    def test_a_hanging_source_does_not_hold_back_the_others(self) -> None:
        started = time.monotonic()
        with (
            mock.patch("urbanlens.dashboard.services.map.nearby_places.NEARBY_DEADLINE", 0.3),
            mock.patch(_LANDMARKS, return_value=[self._landmark()]),
            mock.patch(_WIKIPEDIA) as wikipedia,
        ):
            wikipedia.return_value.get_nearby_articles.side_effect = self._hang
            body = self._nearby().json()

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual([p["name"] for p in body["places"]], ["Old Mill"])
        self.assertEqual(body["incomplete"], ["wikipedia"])
        self.assertFalse(body["cached"])

    def test_a_failed_source_is_not_cached_but_the_others_are(self) -> None:
        article = {"place_id": "wiki_1", "name": "Mill article", "source": "wikipedia"}
        with mock.patch(_LANDMARKS, return_value=[self._landmark()]) as landmarks, mock.patch(_WIKIPEDIA) as wikipedia:
            wikipedia.return_value.get_nearby_articles.side_effect = [GatewayRequestError("down"), [article]]
            failed = self._nearby().json()
            recovered = self._nearby().json()

        self.assertEqual(failed["incomplete"], ["wikipedia"])
        self.assertEqual(recovered["incomplete"], [])
        self.assertEqual(sorted(p["name"] for p in recovered["places"]), ["Mill article", "Old Mill"])
        self.assertEqual(landmarks.call_count, 1, "the landmark answer was cached on the first request")
        self.assertEqual(wikipedia.return_value.get_nearby_articles.call_count, 2)

    def test_a_wikipedia_outage_raises_rather_than_answering_empty(self) -> None:
        import requests

        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

        gateway = WikipediaGateway()
        with (
            mock.patch.object(gateway, "session") as session,
            self.assertRaises(GatewayRequestError),
        ):
            session.get.side_effect = requests.ConnectionError("down")
            gateway.get_nearby_articles(40.0, -74.0)

    def test_non_finite_coordinates_are_refused(self) -> None:
        self.assertEqual(self._nearby(lat="nan").status_code, 400)
        self.assertEqual(self._nearby(lat="91").status_code, 400)
