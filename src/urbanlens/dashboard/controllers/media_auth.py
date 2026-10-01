"""Session-or-credential auth rule for views that serve file bytes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponse

from urbanlens.dashboard.models.account.model import ApiKeyScope

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


#: Cache lifetime for a successfully-authorized media response. Long enough that scrolling a gallery doesn't
#: refetch every thumbnail, short enough that revoking someone's access takes effect promptly in their own
#: browser.
PRIVATE_MEDIA_MAX_AGE_SECONDS = 300


def mark_private_media[ResponseT: HttpResponseBase](response: ResponseT) -> ResponseT:
    """Forbid shared caches from storing a per-viewer media response.

    Django emits ``Vary: Cookie`` on them, which is the correct signal, but it is not a sufficient one -
    plenty of shared caches (CDNs in particular) honour only ``Vary: Accept-Encoding`` and otherwise key
    purely on the URL.

    Args:
        response: A media response that has already passed authorization.

    Returns:
        The same response object, with its cache directives set.
    """
    response["Cache-Control"] = f"private, max-age={PRIVATE_MEDIA_MAX_AGE_SECONDS}"
    return response


class MediaThrottledError(Exception):
    """A credential-authenticated media fetch that exceeded its rate budget.

    Raised out of :meth:`CredentialOrSessionMediaMixin.resolve_media_profile` rather than returned as a
    response, because the throttle verdict arrives in the middle of *identifying* the requester and the
    caller may want to answer it differently (a 429 body, a ``Retry-After``) than it answers "I could
    not identify you at all".
    """


class CredentialOrSessionMediaMixin:
    """Identify the requester of a byte-serving view by session or by credential.

    It never decides whether that person may see the requested file - each view keeps its own
    authorization policy, and a credential holder is put through the byte-for-byte identical policy the
    same person would face while logged in, so holding a key can never reach a file its owner could not.

    Attributes:
        media_scope: The scope a credential must grant to be accepted.
    """

    media_scope: ClassVar[ApiKeyScope] = ApiKeyScope.MEDIA_READ

    def resolve_media_profile(self, request: HttpRequest) -> Profile | None:
        """Identify the profile making this request, by credential or by session.

        **A presented credential wins over an ambient session.** Checking the session first meant a request
        carrying both a cookie and an ``Authorization`` header was served as the *cookie's* account, with
        the credential never authenticated and :attr:`media_scope` never checked - so a WebView sharing the
        site's cookie jar (the mobile client these routes exist for) could fetch media as whichever account
        happened to be logged in, and a token without ``media:read`` bypassed that scope entirely whenever a
        session was also present.

        Args:
            request: The current request.

        Returns:
            The requesting profile, or None when the request is anonymous or
            carries a credential that does not grant: attr:`media_scope`.
            Callers answer None with: meth:`media_auth_failure_response`.

        Raises:
            MediaThrottledError: A valid credential exceeded its media rate budget.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        resolved = self.profile_from_credential(request)
        if resolved is None:
            # No credential was presented (or the one presented is invalid or unscoped, which must not silently
            # fall back to the session's authority).
            if request.META.get("HTTP_AUTHORIZATION"):
                return None
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                profile, _created = Profile.objects.get_or_create(user=user)
                return profile
            return self.profile_from_media_cookie(request)
        credential_user, credential = resolved

        # Metered only on this branch: a session request is already bounded by the site's own login and session
        # handling, while a bearer credential is exactly the thing that could be scripted into a CDN.
        self.enforce_media_throttle(request, credential)

        profile, _created = Profile.objects.get_or_create(user=credential_user)
        return profile

    def profile_from_media_cookie(self, request: HttpRequest) -> Profile | None:
        """Identify the requester from the media-origin cookie.

        Both cookies travel together there (the media cookie carries an explicit ``Domain``), and the media
        cookie is refreshed lazily, so a stale one left over from a previous login would otherwise outrank
        the current session.

        Args:
            request: The current request.

        Returns:
            The profile the cookie authenticates, or None when it is absent, expired, tampered with, or
            names a user that no longer exists.
        """
        from django.contrib.auth import get_user_model

        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.media.origin import MEDIA_COOKIE_NAME, is_media_origin_request, user_id_from_token

        # Only on the media origin. Restricting it to the host it was minted for is what makes "strictly weaker
        # than the session cookie" true rather than aspirational.
        if not is_media_origin_request(request):
            return None
        user_id = user_id_from_token(request.COOKIES.get(MEDIA_COOKIE_NAME, ""))
        if user_id is None:
            return None
        # is_active, so deactivating an account stops serving its media without
        # waiting for the cookie to expire.
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            return None
        profile, _created = Profile.objects.get_or_create(user=user)
        return profile

    def profile_from_credential(self, request: HttpRequest) -> tuple[object, object] | None:
        """Authenticate an API key or OAuth2 token and require :attr:`media_scope`.

        Args:
            request: The current request.

        Returns:
            A ``(user, credential)`` pair when a credential authenticated and grants the required scope,
            else None - including for a perfectly valid...
        """
        from oauth2_provider.contrib.rest_framework import OAuth2Authentication
        from rest_framework.exceptions import AuthenticationFailed
        from rest_framework.request import Request as DrfRequest

        from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
        from urbanlens.dashboard.external_api.permissions import credential_grants

        if not request.META.get("HTTP_AUTHORIZATION"):
            return None

        # The DRF authenticators expect a DRF Request; wrapping keeps them on their supported interface rather
        # than relying on HttpRequest happening to expose enough of it.
        drf_request = DrfRequest(request)
        for authenticator in (ApiKeyAuthentication(), OAuth2Authentication()):
            try:
                result = authenticator.authenticate(drf_request)
            except AuthenticationFailed:
                continue
            if result is None:
                continue
            credential_user, credential = result
            # The same scope check HasApiKeyScope runs, via the shared helper -
            # a second implementation here is exactly how the two would drift.
            if not credential_grants(credential, {self.media_scope}):
                logger.info("Media request rejected: credential lacks %s", self.media_scope.value)
                return None
            return credential_user, credential
        return None

    def enforce_media_throttle(self, request: HttpRequest, credential: object) -> None:
        """Count this fetch against the credential's media budget.

        Args:
            request: The current request; ``auth`` is set on it so the throttle's per-credential cache key
            can be derived exactly as it is for the DRF-served...
            credential: The authenticated credential.

        Raises:
            MediaThrottledError: The credential is over its budget.
        """
        from urbanlens.dashboard.external_api.throttling import ExternalApiMediaThrottle

        request.auth = credential  # type: ignore[attr-defined]
        if not ExternalApiMediaThrottle().allow_request(request, self):
            raise MediaThrottledError

    def media_auth_failure_response(self, request: HttpRequest) -> HttpResponseBase:
        """The response for a request this mixin could not identify.

        Args:
            request: The current request.

        Returns:
            A login redirect for a plain anonymous browser request, so a logged-out user following a
            bookmarked media URL lands somewhere useful; a...

        Raises:
            Http404: The request carried an ``Authorization`` header that did not resolve to a credential
            holding: attr:`media_scope`, or it arrived on the...
        """
        from urbanlens.dashboard.services.media.origin import is_media_origin_request

        # The media origin never serves a page, so there is nothing for a login redirect to accomplish there:
        # the request is an <img>/<video>/<iframe> subresource, and following the redirect would fetch the login
        # page's HTML and render it as a broken image.
        if request.META.get("HTTP_AUTHORIZATION") or is_media_origin_request(request):
            raise Http404
        return redirect_to_login(request.get_full_path(), str(settings.LOGIN_URL))

    def media_throttled_response(self) -> HttpResponse:
        """The response for a credential that exceeded its media budget.

        Returns:
            A plain-text 429.
        """
        return HttpResponse("Too many media requests.", status=429)
