"""Tests for GoogleMapsPhotoProxyView - proxying (and caching) Google Places photo bytes.

An expired photo reference (Google Places photo references aren't valid
forever) must surface as a plain 404 to the client, not a 502 - see
docs/prompts/completed.md's "Handle 502 errors gracefully" entry for the
original report (staging logs showing 404s from Google logged and re-surfaced
as noisy 502s on every view of the same stale reference).
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
from urbanlens.UrbanLens.settings.app import settings


def _http_error(status_code: int, text: str = "") -> requests.exceptions.HTTPError:
    response = mock.Mock(status_code=status_code, text=text)
    return requests.exceptions.HTTPError(response=response)


class GoogleMapsPhotoProxyViewTests(TestCase):
    """GoogleMapsPhotoProxyView.get()."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        cache.clear()
        # The view short-circuits to a 404 before ever calling the gateway
        # when no key is configured - force one so the mocked gateway calls
        # below are actually exercised.
        patcher = mock.patch.object(settings, "google_unrestricted_api_key", "fake-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_expired_reference_returns_404_not_502(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=_http_error(404, "Not Found")):
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 404)

    def test_expired_reference_is_cached_to_avoid_repeat_upstream_calls(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=_http_error(404, "Not Found")) as mocked:
            self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(mocked.call_count, 1)

    def test_other_http_errors_still_return_502(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=_http_error(500, "Server Error")):
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 502)

    def test_connection_failure_returns_502(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=requests.exceptions.ConnectionError("boom")):
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 502)

    def test_successful_fetch_returns_the_image_bytes(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-jpeg-bytes")
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_successful_fetch_is_cached_to_avoid_repeat_upstream_calls(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")) as mocked:
            self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked.call_count, 1)

    def test_external_apis_disabled_blocks_the_upstream_fetch(self) -> None:
        """A requester who opted out of external lookups must not trigger a
        quota-consuming Places API call through this proxy."""
        from urbanlens.dashboard.models.profile.model import Profile

        Profile.objects.filter(user=self.user).update(external_apis_enabled=False)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 404)
        mocked.assert_not_called()

    def test_external_apis_disabled_still_serves_an_already_cached_photo(self) -> None:
        """Cache hits cost no external call, so the opt-out doesn't block them."""
        from urbanlens.dashboard.models.profile.model import Profile

        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        Profile.objects.filter(user=self.user).update(external_apis_enabled=False)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 200)
        mocked.assert_not_called()
