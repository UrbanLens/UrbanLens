"""Bearer-credential access to the Google Maps panel image proxy.

The proxy serves *bytes*, from an ``<img src>``, and was session-only - so
every panel image on a pin was simply unreachable to the mobile client, which
holds an API credential and no session cookie at all. It now shares the
byte-serving authentication rule in
:class:`~urbanlens.dashboard.controllers.media_auth.CredentialOrSessionMediaMixin`
with the media gate.

The four properties that matter, and why each is here rather than left to the
mixin's own tests (which exercise the rule through a throwaway view, and so
cannot see how *this* view sequences it):

1. **The URL signature check still runs first.** ``photo_name`` is entirely
   client-controlled and the proxy will fetch it from Google; the signature is
   what stops the endpoint being an open image-fetching relay against the
   site's own Places quota. If authentication were to run first - or if the
   signature check were moved after it - a valid credential would become a
   licence to fetch arbitrary photo references, which is precisely the failure
   the signature exists to prevent.
2. **Authentication runs before the cache is consulted.** The cache lookup
   returns real bytes; leaving it ahead of the gate would let anyone holding a
   signed URL pull previously-fetched imagery with no credential at all.
3. **An unscoped credential gets 404, not 403** - the no-oracle rule the rest
   of the media surface follows.
4. **The external-lookups opt-out follows the credential's owner.** The check
   used to read ``request.user.profile``, which on a credential-authenticated
   request is the anonymous user and would have raised; it must consult the
   profile the credential resolved to, so a user who opted out of external
   lookups stays opted out through their own API client.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest import mock
from urllib.parse import quote

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.throttling import ExternalApiMediaThrottle
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from django.http import HttpResponse

_PHOTO_NAME = "places/ABC/photos/XYZ"


def _signed_url(photo_name: str = _PHOTO_NAME) -> str:
    """The proxy URL exactly as ``GoogleMapsPhotosPanelSource.media_items`` renders it.

    Args:
        photo_name: The raw (unquoted) Places photo reference.

    Returns:
        The proxy path with a valid ``sig`` query parameter.
    """
    from urbanlens.dashboard.controllers.media_proxy import sign_photo_name

    return (
        reverse("media.google_maps_photo", args=[quote(photo_name, safe="")])
        + f"?sig={quote(sign_photo_name(photo_name), safe='')}"
    )


class MediaProxyCredentialAccessTests(TestCase):
    """``GoogleMapsPhotoProxyView`` under a bearer credential instead of a session."""

    def setUp(self) -> None:
        """A key-holding user, an empty cache, and a configured Places key.

        The first user in a fresh database is auto-promoted to bootstrap site
        admin; a throwaway user absorbs that so the credential owner under test
        is an ordinary account.
        """
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        cache.clear()
        # Without a configured key the view short-circuits to 404 before the
        # gateway is ever reached, which would make every mock below dead code.
        patcher = mock.patch.object(settings, "google_unrestricted_api_key", "fake-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _key_with_scopes(self, scopes: list[ApiKeyScope], user: User | None = None) -> str:
        """Issue an API key carrying exactly *scopes*.

        Args:
            scopes: The scopes to store on the key row.
            user: The key's owner; defaults to the fixture user.

        Returns:
            The raw (unhashed) key value, for use as a bearer token.
        """
        api_key, raw = generate_api_key(user or self.user, "Panel image client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[scope.value for scope in scopes])
        return raw

    def _get(self, url: str, raw_key: str | None = None) -> HttpResponse:
        """Fetch *url*, optionally as a bearer-credential request.

        Args:
            url: The proxy URL to fetch.
            raw_key: A raw API key to present as a bearer token; omitted for an
                anonymous (no ``Authorization`` header) request.

        Returns:
            The proxy's response.
        """
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"} if raw_key else {}
        return self.client.get(url, **headers)

    def test_credential_with_media_read_gets_the_bytes(self) -> None:
        """The whole point: a session-less API client can render panel imagery."""
        raw_key = self._key_with_scopes([ApiKeyScope.MEDIA_READ])
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            response = self._get(_signed_url(), raw_key)
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.content, b"fake-jpeg-bytes")
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_credential_fetch_is_charged_to_the_media_throttle_bucket(self) -> None:
        """One screen of a gallery is dozens of these; unmetered they are free bandwidth.

        Asserts the ``external_api_media`` throttle is the one consulted rather
        than counting requests: how big the budget is belongs to the throttle's
        own tests, while what matters here is that this view participates in it
        at all.
        """
        raw_key = self._key_with_scopes([ApiKeyScope.MEDIA_READ])
        with (
            mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")),
            mock.patch.object(ExternalApiMediaThrottle, "allow_request", return_value=True) as throttle,
        ):
            response = self._get(_signed_url(), raw_key)
        self.assertEqual(response.status_code, HTTPStatus.OK)
        throttle.assert_called_once()

    def test_over_budget_credential_gets_429(self) -> None:
        """A syncing client has to be able to back off rather than see phantom 404s."""
        raw_key = self._key_with_scopes([ApiKeyScope.MEDIA_READ])
        with (
            mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")),
            mock.patch.object(ExternalApiMediaThrottle, "allow_request", return_value=False),
        ):
            response = self._get(_signed_url(), raw_key)
        self.assertEqual(response.status_code, HTTPStatus.TOO_MANY_REQUESTS)

    def test_credential_without_media_read_gets_404_not_403(self) -> None:
        """A valid key with the wrong scopes must not learn the photo exists."""
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ])
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self._get(_signed_url(), raw_key)
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        mocked.assert_not_called()

    def test_garbage_credential_gets_404(self) -> None:
        """An unparseable bearer token is refused the same silent way."""
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self._get(_signed_url(), "not-a-real-key")
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        mocked.assert_not_called()

    def test_anonymous_browser_still_gets_the_login_redirect(self) -> None:
        """Unchanged behaviour: panel image URLs get pasted and bookmarked."""
        response = self._get(_signed_url())
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertIn("/login", response["Location"].lower())

    def test_session_user_is_unaffected(self) -> None:
        """The browser path must not have been traded away for the API path."""
        self.client.force_login(self.user)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            response = self.client.get(_signed_url())
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.content, b"fake-jpeg-bytes")


class MediaProxyOrderingTests(TestCase):
    """Where the credential gate sits relative to the view's own cheaper checks."""

    def setUp(self) -> None:
        """A credential holding ``media:read``, and a configured Places key."""
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        cache.clear()
        patcher = mock.patch.object(settings, "google_unrestricted_api_key", "fake-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        api_key, self.raw_key = generate_api_key(self.user, "Panel image client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.MEDIA_READ.value])

    def test_signature_is_checked_before_the_credential_is_even_read(self) -> None:
        """A credential is not a licence to fetch arbitrary photo references.

        The signature is the only thing binding a proxy URL to a photo name the
        server itself issued; if authentication were allowed to run first and
        satisfy the request, any key holder could burn the site's Places quota
        on guessed or copied references.
        """
        unsigned = reverse("media.google_maps_photo", args=[quote(_PHOTO_NAME, safe="")])
        with (
            mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked,
            mock.patch.object(ExternalApiMediaThrottle, "allow_request", return_value=True) as throttle,
        ):
            response = self.client.get(unsigned, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        mocked.assert_not_called()
        # Not merely "refused": refused without paying for authentication or a
        # throttle round-trip, which is why the mixin does not gate dispatch().
        throttle.assert_not_called()

    def test_anonymous_request_cannot_read_an_already_cached_photo(self) -> None:
        """Authentication runs ahead of the cache, which serves real bytes.

        Regression guard for the obvious way to write this change: leaving the
        gate where the old profile lookup was (below the cache read) would turn
        a signed URL alone into anonymous access to any photo someone else had
        already fetched.
        """
        self.client.force_login(self.user)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")):
            self.assertEqual(self.client.get(_signed_url()).status_code, HTTPStatus.OK)
        self.client.logout()

        response = self.client.get(_signed_url())
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertNotIn(b"fake-jpeg-bytes", response.content)


class MediaProxyExternalApiOptOutTests(TestCase):
    """The external-lookups opt-out, evaluated against the credential's owner."""

    def setUp(self) -> None:
        """A credential holder who has opted out of external API lookups."""
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        cache.clear()
        patcher = mock.patch.object(settings, "google_unrestricted_api_key", "fake-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        api_key, self.raw_key = generate_api_key(self.user, "Panel image client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.MEDIA_READ.value])

    def test_opted_out_credential_owner_does_not_trigger_an_upstream_fetch(self) -> None:
        """The opt-out is a property of the person, not of the browser they used."""
        Profile.objects.filter(user=self.user).update(external_apis_enabled=False)
        with mock.patch.object(GooglePlacesGateway, "get_photo_media") as mocked:
            response = self.client.get(_signed_url(), HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        mocked.assert_not_called()

    def test_opted_in_credential_owner_still_fetches(self) -> None:
        """The mirror image, so the test above can't pass by refusing everyone."""
        with mock.patch.object(
            GooglePlacesGateway, "get_photo_media", return_value=(b"fake-jpeg-bytes", "image/jpeg")
        ) as mocked:
            response = self.client.get(_signed_url(), HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(response.status_code, HTTPStatus.OK)
        mocked.assert_called_once()
