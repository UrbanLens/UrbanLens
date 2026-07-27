"""Shared bases for endpoints reachable by *either* a browser session or a credential.

Most of this package is credential-only: ``ExternalApiView`` accepts an
``ApiKey``/OAuth2 token and nothing else, and an ordinary logged-in browser
request cannot reach it (see the package docstring). A handful of endpoints
have to serve both audiences from one URL, though - the E2EE key-storage
views in ``controllers.e2ee`` are the site's own web client's key API *and*
the mobile client's, and duplicating them under a second prefix would mean
two implementations of the same delicate key-exchange contract drifting
apart.

:class:`DualAuthJsonView` is that seam. It is deliberately *not* the default:
an endpoint should only opt in when the web client genuinely needs the same
URL, because every dual-auth endpoint is one more place where the credential
boundary depends on a per-view scope declaration rather than on the package
boundary itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope
from urbanlens.dashboard.external_api.throttling import ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.account.model import ApiKeyScope


class IsSessionAuthenticated(BasePermission):
    """Permits a request authenticated by a browser session rather than a credential.

    ``request.auth`` is the discriminator: every credential authenticator in
    this package stashes the ``ApiKey``/``AccessToken`` there, while
    ``SessionAuthentication`` sets a user and leaves ``auth`` as None. A
    session is therefore exactly the ``auth is None`` case.

    This exists because :class:`~urbanlens.dashboard.external_api.permissions.HasApiKeyScope`
    fails closed on ``auth is None`` - which is correct and must stay that
    way. Relaxing *it* to treat a missing credential as "no scope needed"
    would open every endpoint in this package to any logged-in session and
    destroy the boundary the package exists to draw; the narrow fix is a
    separate permission that only the deliberately dual-auth views OR in.

    Note:
        This grants no authority of its own beyond "is a logged-in session".
        The views composing it still apply their own ownership checks (a
        session can only ever reach its own key bundle), exactly as they did
        when they were ``LoginRequiredMixin`` Django views.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return True when this request carries a logged-in session and no credential.

        Args:
            request: The incoming DRF request.
            view: The view handling it (unused; required by the interface).

        Returns:
            True for a session-authenticated request, False when a credential
            authenticated it or nobody did.
        """
        user = getattr(request, "user", None)
        return request.auth is None and bool(user and user.is_authenticated)


class DualAuthJsonView(APIView):
    """A JSON endpoint reachable by browser cookies *or* an API key / OAuth2 token.

    Authentication is tried session-first, so an ordinary page-load fetch from
    the logged-in web client behaves exactly as it did before this view was a
    DRF view - including CSRF, which ``SessionAuthentication`` continues to
    enforce on unsafe methods. A credential-bearing request carries no session,
    falls through to the credential authenticators, and is correctly exempt
    from CSRF (it never relies on ambient browser authority).

    The permission expresses "authenticated, and then either a credential
    carrying the right scopes or a plain session":
    ``IsAuthenticated & (HasApiKeyScope | IsSessionAuthenticated)``. The
    ``IsAuthenticated`` conjunct is not redundant - it produces the 401/403
    distinction for an anonymous caller before either branch is consulted.

    Subclasses declare scopes per HTTP method in ``required_scopes_by_method``.
    That declaration only ever *restricts* credential callers: ``HasApiKeyScope``
    fails closed when a method has no entry, so a credential can never reach a
    method whose requirements nobody declared, while a session caller is
    unaffected by it either way.
    """

    #: Session first: the web client's existing cookie flow must keep working
    #: unchanged, and a credential request carries no session cookie to match.
    authentication_classes = [SessionAuthentication, ApiKeyAuthentication, OAuth2Authentication]
    permission_classes = [IsAuthenticated & (HasApiKeyScope | IsSessionAuthenticated)]
    #: Same tiered per-credential caps the rest of the package uses. These are
    #: inert for session callers by construction - ``get_cache_key`` returns
    #: None without a credential, which is DRF's "don't throttle" signal - so
    #: adding them here cannot rate-limit the web UI.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle]
    #: JSON only. DRF's default renderer list would also offer the browsable
    #: HTML API, which would start serving an HTML page to anything sending
    #: ``Accept: text/html`` (a browser address bar, a link preview fetcher) -
    #: a change from the unconditional JSON these endpoints returned as plain
    #: Django views, and not a page that should exist for key material.
    renderer_classes = [JSONRenderer]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {}

    @property
    def required_scopes(self) -> frozenset[ApiKeyScope]:
        """The scopes the current request's HTTP method requires of a credential caller."""
        return self.required_scopes_by_method.get(self.request.method or "", frozenset())
