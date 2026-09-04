"""Guards on ``CredentialOrSessionMediaMixin``, the byte-serving auth rule.

Three views hand a user actual file bytes - the authenticated media gate, the
panel image proxy, and the SpotGuessr round image - and all three must accept
"a logged-in session, or a bearer credential holding ``media:read``" without
accepting anything else. The rule now lives in one mixin, so these tests
exercise it directly (through a throwaway view that does nothing but call it)
rather than only through the media gate, which layers its own per-file
authorization on top and would mask a change in the authentication half.

What is actually being held:

1. **A credential lacking ``media:read`` gets nothing** - and gets it as a 404,
   not a 403, so a bearer token cannot be used to discover which files exist.
2. **An anonymous browser request still gets the login redirect**, because
   media URLs get bookmarked and pasted; only a request carrying an
   ``Authorization`` header (an API client, which cannot use an HTML form) is
   answered with a bare 404 instead.
3. **The scope check is the shared one**, so the PAT-versus-OAuth2 rules apply
   here exactly as they do to every DRF endpoint.
4. **The mixin does not gate ``dispatch()``**, so a view with a cheaper,
   more specific pre-check (the image proxy's URL signature) can still run it
   first instead of paying for authentication on requests it is about to throw
   away.
5. **The media gate delegates rather than duplicating** - the extraction is
   worthless if the original kept its own copy.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.http import Http404, HttpResponse
from django.test import RequestFactory
from django.views import View
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.media import MediaGateView
from urbanlens.dashboard.controllers.media_auth import CredentialOrSessionMediaMixin, MediaThrottledError
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

if TYPE_CHECKING:
    from django.http import HttpRequest


class _ProbeView(CredentialOrSessionMediaMixin, View):
    """A minimal consumer of the mixin, standing in for the future views.

    Deliberately has no authorization policy of its own: the point is to
    observe the authentication half in isolation, the way the panel image proxy
    and the SpotGuessr round image will use it once a signature or session
    check of their own has already passed.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Identify the requester and report the outcome as a status code.

        Args:
            request: The current request.

        Returns:
            200 naming the resolved profile, a login redirect for an anonymous
            browser, or 429 for a throttled credential.

        Raises:
            Http404: A credential-bearing request that could not be resolved.
        """
        try:
            profile = self.resolve_media_profile(request)
        except MediaThrottledError:
            return self.media_throttled_response()
        if profile is None:
            return self.media_auth_failure_response(request)
        return HttpResponse(str(profile.pk))


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token.

    Args:
        raw_key: The raw (unhashed) key value.

    Returns:
        Header kwargs for ``RequestFactory``/``Client``.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class MediaAuthDelegationTests(SimpleTestCase):
    """The media gate must use the extraction, not keep a private copy."""

    def test_media_gate_inherits_the_mixin(self) -> None:
        """``MediaGateView`` is repointed rather than duplicated."""
        self.assertTrue(issubclass(MediaGateView, CredentialOrSessionMediaMixin))

    def test_media_gate_does_not_redefine_the_resolution_methods(self) -> None:
        """A local override would silently reintroduce the copy this removed.

        Checking ``__dict__`` rather than ``hasattr`` is the point: inherited
        attributes are exactly what should be present, and only a re-definition
        in the subclass body would mean the two implementations had come back.
        """
        for name in (
            "resolve_media_profile",
            "profile_from_credential",
            "enforce_media_throttle",
            "media_auth_failure_response",
        ):
            self.assertNotIn(name, MediaGateView.__dict__)

    def test_mixin_does_not_gate_dispatch(self) -> None:
        """Authentication must stay an explicit call the view chooses to make.

        The panel image proxy rejects an unsigned ``sig`` before touching the
        cache, a database row, or the upstream API. A mixin that authenticated
        inside ``dispatch()`` would reorder that and spend a ``get_or_create``
        plus a throttle lookup on requests that are about to be discarded - so
        overriding ``dispatch`` here is a regression, not an optimization.
        """
        self.assertNotIn("dispatch", CredentialOrSessionMediaMixin.__dict__)

    def test_default_scope_is_media_read(self) -> None:
        """``media:read`` is what users actually consented to for file access."""
        self.assertEqual(CredentialOrSessionMediaMixin.media_scope, ApiKeyScope.MEDIA_READ)


class MediaAuthResolutionTests(TestCase):
    """Who the mixin identifies, and how it refuses everyone else."""

    def setUp(self) -> None:
        """Create a key owner and the throwaway view under test."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="fetcher")
        self.profile = Profile.objects.get(user=self.user)
        self.factory = RequestFactory()
        self.view = _ProbeView()

    def _key_with_scopes(self, scopes: list[ApiKeyScope], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes*.

        Args:
            scopes: Scopes to store on the row.
            user: Owner of the key; defaults to the fixture user.

        Returns:
            The raw key value.
        """
        api_key, raw = generate_api_key(user or self.user, "Media client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[scope.value for scope in scopes])
        return raw

    def _get(self, **extra) -> HttpResponse:
        """Run the probe view against a request built with *extra* headers.

        Args:
            **extra: Extra WSGI environ entries, e.g. an Authorization header.

        Returns:
            The probe view's response.
        """
        request = self.factory.get("/probe/", **extra)
        request.user = AnonymousUser()
        return self.view.get(request)

    def test_credential_with_media_read_resolves_its_owner(self) -> None:
        """The happy path: a scoped key identifies exactly one profile."""
        response = self._get(**_bearer(self._key_with_scopes([ApiKeyScope.MEDIA_READ])))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.content.decode(), str(self.profile.pk))

    def test_credential_without_media_read_gets_404_not_403(self) -> None:
        """A valid-but-unscoped credential must not be told it was forbidden.

        403 would confirm the credential is real and the path exists; media
        authorization is no-oracle end to end, so an unscoped key is answered
        exactly as a missing file is.
        """
        raw_key = self._key_with_scopes([ApiKeyScope.PHOTOS_READ])
        with self.assertRaises(Http404):
            self._get(**_bearer(raw_key))

    def test_garbage_credential_gets_404(self) -> None:
        """An unparseable bearer token is refused the same silent way."""
        with self.assertRaises(Http404):
            self._get(**_bearer("not-a-real-key"))

    def test_anonymous_with_no_header_gets_the_login_redirect(self) -> None:
        """Browser UX is preserved: a logged-out visitor lands on the login page."""
        response = self._get()
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertIn("/login", response["Location"].lower())

    def test_session_user_resolves_without_any_scope(self) -> None:
        """A browser session needs no credential and no scope at all."""
        request = self.factory.get("/probe/")
        request.user = self.user
        response = self.view.get(request)
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.content.decode(), str(self.profile.pk))

    def test_a_presented_credential_wins_over_an_ambient_session(self) -> None:
        """A request carrying both is served as the *credential's* account.

        The reverse (session first) let a WebView sharing the site's cookie
        jar fetch media as whichever account happened to be logged in, and
        skipped the scope check entirely whenever a session was also present -
        see ``resolve_media_profile``'s own docstring.
        """
        stranger = baker.make(User, username="someone-else")
        raw_key = self._key_with_scopes([ApiKeyScope.MEDIA_READ], user=stranger)

        request = self.factory.get("/probe/", **_bearer(raw_key))
        request.user = self.user
        response = self.view.get(request)

        self.assertEqual(response.content.decode(), str(stranger.profile.pk))

    def test_an_unscoped_credential_cannot_fall_back_to_the_session(self) -> None:
        """The scope check can't be bypassed by also being logged in.

        This is the half of the ordering fix with teeth: a token deliberately
        issued without ``media:read`` must be refused outright, not quietly
        served under the cookie's authority.
        """
        stranger = baker.make(User, username="unscoped-holder")
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ], user=stranger)

        request = self.factory.get("/probe/", **_bearer(raw_key))
        request.user = self.user

        # Http404 is raised rather than returned, so it flows through the
        # view's normal 404 handling - see media_auth_failure_response.
        with self.assertRaises(Http404):
            self.view.get(request)

    def test_credential_resolves_a_profile_that_does_not_exist_yet(self) -> None:
        """An OAuth2-only account with no profile row is still identified."""
        fresh_user = baker.make(User, username="api-only")
        raw_key = self._key_with_scopes([ApiKeyScope.MEDIA_READ], user=fresh_user)
        Profile.objects.filter(user=fresh_user).delete()

        response = self._get(**_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(Profile.objects.filter(user=fresh_user).count(), 1)

    def test_throttled_credential_gets_429_not_404(self) -> None:
        """An over-budget credential is told to slow down, not that files vanished.

        The caller has already proven it holds a valid scoped credential, so
        429 leaks nothing - and it is the only answer that lets a syncing
        client back off instead of concluding every file is missing.
        """
        raw_key = self._key_with_scopes([ApiKeyScope.MEDIA_READ])
        request = self.factory.get("/probe/", **_bearer(raw_key))
        request.user = AnonymousUser()

        # Patched rather than driven by thousands of real requests: the size of
        # the budget belongs to the throttle's own tests, while what matters
        # here is which response the mixin's caller produces when it trips.
        with mock.patch.object(_ProbeView, "enforce_media_throttle", side_effect=MediaThrottledError):
            response = self.view.get(request)

        self.assertEqual(response.status_code, HTTPStatus.TOO_MANY_REQUESTS)

    def test_throttle_is_not_charged_to_session_requests(self) -> None:
        """A browser session never reaches the metered branch at all.

        The site's own login and session handling already bounds it; charging
        it against a per-credential budget would let a heavy page of thumbnails
        rate-limit the web UI.
        """
        request = self.factory.get("/probe/")
        request.user = self.user

        with mock.patch.object(_ProbeView, "enforce_media_throttle", side_effect=MediaThrottledError) as throttle:
            response = self.view.get(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        throttle.assert_not_called()
