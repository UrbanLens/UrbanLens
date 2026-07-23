"""Per-credential scope enforcement for the external API.

Distinct from Django's user permissions and from the internal API's
``IsAuthenticated`` default: a credential can only do what its own scope
grant allows, regardless of what the underlying user account could do if it
were logged in normally through the site. Two credential kinds are honored,
sharing one scope vocabulary (``ApiKeyScope`` values, mirrored into
``OAUTH2_PROVIDER["SCOPES"]``):

- ``ApiKey`` (PAT-style, ``scopes`` JSON list) - simple integrations.
- django-oauth-toolkit ``AccessToken`` (space-separated ``scope`` string,
  ``allow_scopes()``) - native clients using OAuth2 + PKCE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


class HasApiKeyScope(BasePermission):
    """Requires the authenticating credential to grant every scope in ``view.required_scopes``.

    Views using this must define ``required_scopes`` (an attribute or
    property yielding a set of
    :class:`~urbanlens.dashboard.models.account.model.ApiKeyScope` values).
    A view with an empty/missing ``required_scopes`` is always denied rather
    than treated as "no scope needed" - an endpoint added here without
    remembering to set it should fail closed, not open.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Check that ``request.auth`` (ApiKey or OAuth2 AccessToken) grants the view's required scopes."""
        credential = request.auth
        required_scopes: frozenset[str] = getattr(view, "required_scopes", frozenset())
        if credential is None or not required_scopes:
            return False
        # django-oauth-toolkit AccessToken - validity (expiry/revocation) was
        # already established by OAuth2Authentication; only scopes remain.
        if hasattr(credential, "allow_scopes"):
            return credential.allow_scopes(list(required_scopes))
        return set(required_scopes).issubset(set(getattr(credential, "scopes", ())))
