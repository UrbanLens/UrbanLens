"""The "browser session OR bearer credential" rule for views that serve bytes.

Three views hand a user actual file bytes rather than JSON - the authenticated
media gate (``controllers.media.MediaGateView``), the panel image proxy, and
the SpotGuessr round image - and all three have the same authentication
problem. They are ordinary Django ``View``s that a logged-in browser hits
directly from an ``<img src>``, so they cannot be moved into the external API
package and cannot use a DRF permission class; but the mobile client fetches
exactly the same bytes with a bearer credential and no session cookie at all,
so ``LoginRequiredMixin`` alone locks the app out of its own images.

This module holds that rule once. Three copies of "try the session, else run
the DRF authenticators by hand, else refuse" is three chances to get one of
the details below subtly wrong, and each of them fails *open* in a way that
looks fine in a browser:

- **The scope check must be the shared one.** A credential is accepted only
  when :func:`~urbanlens.dashboard.external_api.permissions.credential_grants`
  says it holds ``media:read``. That function - not a local
  ``"media:read" in key.scopes`` - is what applies the PAT-versus-OAuth2 rules,
  so a re-implementation here would drift from the rest of the API the moment
  those rules change.
- **A failed credential is a 404, never a 403.** Media authorization is
  no-oracle throughout: a caller must not be able to tell "this file is not
  yours" from "this file does not exist", and a 403 on an unscoped credential
  would answer that question for them.
- **An anonymous browser still gets the login redirect.** Media URLs are
  pasted into address bars and followed from bookmarks; turning that into a
  404 would strand users who are simply logged out. The discriminator is the
  ``Authorization`` header: a request that carries one is an API client, and
  redirecting an API client to an HTML login form is useless *and* confirms
  the path exists.
- **Only the credential branch is metered.** A session request is already
  bounded by the site's login and session handling; a bearer credential is
  exactly the thing that gets scripted into a CDN backfill, so it is charged
  against ``ExternalApiMediaThrottle``.

Deliberately *not* a ``dispatch()``-level gate. Views using this often have a
cheaper, more specific check that has to run first - the panel image proxy
rejects an unsigned/tampered ``sig`` parameter before it will touch the cache,
a database row, or the upstream API - and a mixin that authenticated inside
``dispatch()`` would reorder that, spending a ``get_or_create`` and a throttle
lookup on requests that are about to be thrown away. So the mixin exposes
methods the view calls where it wants them, and the view keeps control of its
own ordering.
"""

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


#: Cache lifetime for a successfully-authorized media response. Long enough
#: that scrolling a gallery doesn't refetch every thumbnail, short enough that
#: revoking someone's access takes effect promptly in their own browser.
PRIVATE_MEDIA_MAX_AGE_SECONDS = 300


def mark_private_media[ResponseT: HttpResponseBase](response: ResponseT) -> ResponseT:
    """Forbid shared caches from storing a per-viewer media response.

    Every response these views produce is authorized *per viewer*: the same
    URL legitimately yields bytes for one profile and a 404 for another. Django
    emits ``Vary: Cookie`` on them, which is the correct signal, but it is not
    a sufficient one - plenty of shared caches (CDNs in particular) honour only
    ``Vary: Accept-Encoding`` and otherwise key purely on the URL. Media URLs
    here end in real image extensions, which is exactly what extension-based
    CDN cache rules match, and with no ``Cache-Control`` at all such a cache
    falls back to its own default TTL. ``private`` is the directive that
    actually forbids shared storage; the ``max-age`` keeps the requester's own
    browser cache, which is per-user and therefore safe.

    Generic in the response type so callers keep whatever they passed in - a
    ``FileResponse`` stays a ``FileResponse``. Annotating this as
    ``HttpResponseBase`` would silently widen the return type of every view that
    wraps its response in this, which mypy then rejects at the call site.

    Args:
        response: A media response that has already passed authorization.

    Returns:
        The same response object, with its cache directives set.
    """
    response["Cache-Control"] = f"private, max-age={PRIVATE_MEDIA_MAX_AGE_SECONDS}"
    return response


class MediaThrottledError(Exception):
    """A credential-authenticated media fetch that exceeded its rate budget.

    Raised out of :meth:`CredentialOrSessionMediaMixin.resolve_media_profile`
    rather than returned as a response, because the throttle verdict arrives in
    the middle of *identifying* the requester and the caller may want to answer
    it differently (a 429 body, a ``Retry-After``) than it answers "I could not
    identify you at all".
    """


class CredentialOrSessionMediaMixin:
    """Identify the requester of a byte-serving view by session or by credential.

    Resolves *who is asking* and nothing more. It never decides whether that
    person may see the requested file - each view keeps its own authorization
    policy, and a credential holder is put through the byte-for-byte identical
    policy the same person would face while logged in, so holding a key can
    never reach a file its owner could not.

    Typical use, preserving the view's own ordering::

        class SomeImageView(CredentialOrSessionMediaMixin, View):
            def get(self, request, ...):
                if not signature_ok(...):      # cheap, specific, runs first
                    return HttpResponse(status=404)
                try:
                    profile = self.resolve_media_profile(request)
                except MediaThrottledError:
                    return self.media_throttled_response()
                if profile is None:
                    return self.media_auth_failure_response(request)
                ...

    Attributes:
        media_scope: The scope a credential must grant to be accepted. Kept as
            a class attribute so a future byte-serving view with a different
            requirement can retarget it without forking the resolution logic,
            but it should stay ``media:read`` for anything serving user files -
            that is the scope users actually consented to on the OAuth2 screen
            when they agreed to let an app fetch their images.
    """

    media_scope: ClassVar[ApiKeyScope] = ApiKeyScope.MEDIA_READ

    def resolve_media_profile(self, request: HttpRequest) -> Profile | None:
        """Identify the profile making this request, by credential or by session.

        **A presented credential wins over an ambient session.** Checking the
        session first meant a request carrying both a cookie and an
        ``Authorization`` header was served as the *cookie's* account, with the
        credential never authenticated and :attr:`media_scope` never checked -
        so a WebView sharing the site's cookie jar (the mobile client these
        routes exist for) could fetch media as whichever account happened to be
        logged in, and a token without ``media:read`` bypassed that scope
        entirely whenever a session was also present. It also skipped the
        per-credential throttle below, since that only runs on the credential
        branch.

        Only a request with no ``Authorization`` header at all falls through to
        the session, so the ordinary browser flow is unchanged.

        Args:
            request: The current request.

        Returns:
            The requesting profile, or None when the request is anonymous or
            carries a credential that does not grant :attr:`media_scope`.
            Callers answer None with :meth:`media_auth_failure_response`.

        Raises:
            MediaThrottledError: A valid credential exceeded its media rate
                budget.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        resolved = self.profile_from_credential(request)
        if resolved is None:
            # No credential was presented (or the one presented is invalid or
            # unscoped, which must not silently fall back to the session's
            # authority). Only a genuinely credential-free request may use the
            # cookie.
            if request.META.get("HTTP_AUTHORIZATION"):
                return None
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                profile, _created = Profile.objects.get_or_create(user=user)
                return profile
            return self.profile_from_media_cookie(request)
        credential_user, credential = resolved

        # Metered only on this branch: a session request is already bounded by
        # the site's own login and session handling, while a bearer credential
        # is exactly the thing that could be scripted into a CDN.
        self.enforce_media_throttle(request, credential)

        profile, _created = Profile.objects.get_or_create(user=credential_user)
        return profile

    def profile_from_media_cookie(self, request: HttpRequest) -> Profile | None:
        """Identify the requester from the media-origin cookie.

        The last resort, reached only by a request with no ``Authorization``
        header and no session - which on the media origin is every request, since
        the session cookie is host-only for the app's hostname and never sent
        there. See :mod:`urbanlens.dashboard.services.media.origin`.

        Accepted **only** on the media origin, never on the app's own hostname.

        It is checked *after* the session rather than before so that a request on
        the app's own origin is still served as its session's account. Both
        cookies travel together there (the media cookie carries an explicit
        ``Domain``), and the media cookie is refreshed lazily, so a stale one left
        over from a previous login would otherwise outrank the current session.

        Args:
            request: The current request.

        Returns:
            The profile the cookie authenticates, or None when it is absent,
            expired, tampered with, or names a user that no longer exists.
        """
        from django.contrib.auth import get_user_model

        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.media.origin import MEDIA_COOKIE_NAME, is_media_origin_request, user_id_from_token

        # Only on the media origin. The cookie carries an explicit Domain (it has
        # to, or it would never reach that host), so it is also sent back to the
        # app origin - where accepting it would quietly make it a second, longer-
        # lived session: it outlives the real one by up to its own 12 hours, and
        # every CredentialOrSessionMediaMixin view would take it, including the
        # panel image proxy, whose upstream fetches are billed. Restricting it to
        # the host it was minted for is what makes "strictly weaker than the
        # session cookie" true rather than aspirational.
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
            A ``(user, credential)`` pair when a credential authenticated and
            grants the required scope, else None - including for a perfectly
            valid credential that simply lacks the scope, which the caller then
            reports as "not found" rather than "forbidden".
        """
        from oauth2_provider.contrib.rest_framework import OAuth2Authentication
        from rest_framework.exceptions import AuthenticationFailed
        from rest_framework.request import Request as DrfRequest

        from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
        from urbanlens.dashboard.external_api.permissions import credential_grants

        if not request.META.get("HTTP_AUTHORIZATION"):
            return None

        # The DRF authenticators expect a DRF Request; wrapping keeps them on
        # their supported interface rather than relying on HttpRequest
        # happening to expose enough of it.
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
            request: The current request; ``auth`` is set on it so the
                throttle's per-credential cache key can be derived exactly as
                it is for the DRF-served endpoints.
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
            A login redirect for a plain anonymous browser request, so a
            logged-out user following a bookmarked media URL lands somewhere
            useful; a bare 404 for anything carrying an ``Authorization``
            header, since an API client cannot use an HTML login form and the
            redirect would itself confirm the path exists.

        Raises:
            Http404: The request carried an ``Authorization`` header that did
                not resolve to a credential holding :attr:`media_scope`, or it
                arrived on the media origin. Raised rather than returned so it
                flows through the view's normal 404 handling alongside every
                other media denial - a distinct response object here would be a
                distinguishable failure mode, which is precisely the oracle this
                gate avoids.
        """
        from urbanlens.dashboard.services.media.origin import is_media_origin_request

        # The media origin never serves a page, so there is nothing for a login
        # redirect to accomplish there: the request is an <img>/<video>/<iframe>
        # subresource, and following the redirect would fetch the login page's
        # HTML and render it as a broken image. Worse, the login page would be
        # framed from a foreign origin. A 404 fails the subresource cleanly and
        # leaves the app origin - where the user actually is - to handle the
        # logged-out state.
        if request.META.get("HTTP_AUTHORIZATION") or is_media_origin_request(request):
            raise Http404
        return redirect_to_login(request.get_full_path(), str(settings.LOGIN_URL))

    def media_throttled_response(self) -> HttpResponse:
        """The response for a credential that exceeded its media budget.

        Returns:
            A plain-text 429. Not a 404: the caller has already proven it holds
            a valid credential, so telling it to slow down leaks nothing and is
            the only thing that lets a syncing client back off correctly rather
            than treating every file as missing.
        """
        return HttpResponse("Too many media requests.", status=429)
