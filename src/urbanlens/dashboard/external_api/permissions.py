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

from urbanlens.dashboard.models.account.model import ApiKeyScope

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rest_framework.request import Request
    from rest_framework.views import APIView

#: Scopes a PAT-style ``ApiKey`` may never exercise, only a user-consented
#: OAuth2 client.
#:
#: Direct messages are end-to-end encrypted: reading or sending them requires
#: per-device key material that a long-lived, server-side credential model
#: simply does not have, so a PAT could at best reach ciphertext envelopes it
#: cannot open. More importantly, an API key is a bearer secret that tends to
#: end up in CI configs, scripts and screenshots - a leaked one must not become
#: a path into someone's DMs. OAuth2 tokens are bound to a registered client,
#: expire in an hour, and are minted only after a consent screen that names
#: this capability explicitly.
OAUTH2_ONLY_SCOPES = frozenset({ApiKeyScope.MESSAGES_READ, ApiKeyScope.MESSAGES_WRITE})


def credential_grants(credential: object | None, scopes: Iterable[str]) -> bool:
    """Check whether one resolved credential grants every scope in *scopes*.

    The single implementation of the "does this credential allow that?"
    question, shared by :class:`HasApiKeyScope` (the DRF permission every
    external endpoint runs) and by non-DRF callers that resolve a credential
    by hand - notably ``controllers.media.MediaGateView``, a plain Django
    ``View`` that cannot use a DRF permission class but must apply the exact
    same rule. Duplicating the branch there would let the two drift, which for
    a scope check means one of them silently getting more permissive.

    Args:
        credential: The authenticated credential - a PAT-style ``ApiKey``, a
            django-oauth-toolkit ``AccessToken``, or None for an
            unauthenticated request.
        scopes: The scopes the caller must hold. An empty collection is
            refused rather than treated as "nothing required", so a caller
            that computes its requirement dynamically and comes up empty
            fails closed.

    Returns:
        True when *credential* grants every requested scope.
    """
    required = frozenset(scopes)
    if credential is None or not required:
        return False
    # django-oauth-toolkit AccessToken - validity (expiry/revocation) was
    # already established by OAuth2Authentication; only scopes remain.
    if hasattr(credential, "allow_scopes"):
        return bool(credential.allow_scopes(list(required)))
    # PAT-style ApiKey. Refuse the OAuth2-only scopes even if one somehow
    # carries them (hand-edited row, a future scope picker with a bug):
    # the restriction is about the credential *kind*, so it is enforced
    # here rather than left to whatever wrote the grant.
    if OAUTH2_ONLY_SCOPES & set(required):
        return False
    return required.issubset(set(getattr(credential, "scopes", ())))


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
        return credential_grants(request.auth, getattr(view, "required_scopes", frozenset()))
