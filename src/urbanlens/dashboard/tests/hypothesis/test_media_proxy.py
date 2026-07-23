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


def _signed_url(photo_name: str) -> str:
    """The proxy URL exactly as GoogleMapsPhotosPanelSource.media_items renders it."""
    from urllib.parse import quote

    from urbanlens.dashboard.controllers.media_proxy import sign_photo_name

    return reverse("media.google_maps_photo", args=[quote(photo_name, safe="")]) + f"?sig={quote(sign_photo_name(photo_name), safe='')}"


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
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 404)

    def test_expired_reference_is_cached_to_avoid_repeat_upstream_calls(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=_http_error(404, "Not Found")) as mocked:
            self.client.get(_signed_url("places/ABC/photos/XYZ"))
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(mocked.call_count, 1)

    def test_other_http_errors_still_return_502(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=_http_error(500, "Server Error")):
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 502)

    def test_connection_failure_returns_502(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", side_effect=requests.exceptions.ConnectionError("boom")):
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 502)

    def test_successful_fetch_returns_the_image_bytes(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-jpeg-bytes")
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_successful_fetch_is_cached_to_avoid_repeat_upstream_calls(self) -> None:
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")) as mocked:
            self.client.get(_signed_url("places/ABC/photos/XYZ"))
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked.call_count, 1)

    def test_unsigned_request_is_rejected_before_any_upstream_call(self) -> None:
        """The photo_name path segment is client-controlled - without a valid
        signature, a replayed/guessed reference must not consume Places quota."""
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 404)
        mocked.assert_not_called()

    def test_signature_for_a_different_photo_name_is_rejected(self) -> None:
        from urbanlens.dashboard.controllers.media_proxy import sign_photo_name

        wrong_sig = sign_photo_name("places/OTHER/photos/DIFFERENT")
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]) + f"?sig={wrong_sig}")
        self.assertEqual(response.status_code, 404)
        mocked.assert_not_called()

    def test_unsigned_request_never_serves_even_a_cached_photo(self) -> None:
        """The signature check runs before the cache lookup - an invalid URL
        reveals nothing about what's cached."""
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            self.client.get(_signed_url("places/ABC/photos/XYZ"))
        response = self.client.get(reverse("media.google_maps_photo", args=["places/ABC/photos/XYZ"]))
        self.assertEqual(response.status_code, 404)

    def test_media_items_renders_urls_the_proxy_accepts(self) -> None:
        """End-to-end contract: the gallery's own rendered URL passes the check."""
        from urbanlens.dashboard.plugins.builtin.google_places import GoogleMapsPhotosPanelSource

        items = GoogleMapsPhotosPanelSource().media_items({"place_id": "p1", "photo_names": ["places/ABC/photos/XYZ"]})
        self.assertEqual(len(items), 1)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            response = self.client.get(items[0].url)
        self.assertEqual(response.status_code, 200)

    def test_external_apis_disabled_blocks_the_upstream_fetch(self) -> None:
        """A requester who opted out of external lookups must not trigger a
        quota-consuming Places API call through this proxy."""
        from urbanlens.dashboard.models.profile.model import Profile

        Profile.objects.filter(user=self.user).update(external_apis_enabled=False)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 404)
        mocked.assert_not_called()

    def test_external_apis_disabled_still_serves_an_already_cached_photo(self) -> None:
        """Cache hits cost no external call, so the opt-out doesn't block them."""
        from urbanlens.dashboard.models.profile.model import Profile

        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            self.client.get(_signed_url("places/ABC/photos/XYZ"))
        Profile.objects.filter(user=self.user).update(external_apis_enabled=False)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(_signed_url("places/ABC/photos/XYZ"))
        self.assertEqual(response.status_code, 200)
        mocked.assert_not_called()
