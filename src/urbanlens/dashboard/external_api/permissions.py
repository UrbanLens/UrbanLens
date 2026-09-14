"""Per-credential scope enforcement for the external API.

Distinct from Django's user permissions and from the internal API's ``IsAuthenticated`` default: a
credential can only do what its own scope grant allows, regardless of what the underlying user
account could do if it were logged in normally through the site.
Two credential kinds are honored, sharing one scope vocabulary (``ApiKeyScope`` values, mirrored
into ``OAUTH2_PROVIDER["SCOPES"]``):

- ``ApiKey`` (PAT-style, ``scopes`` JSON list) - simple integrations.
- django-oauth-toolkit ``AccessToken`` (space-separated ``scope`` string, ``allow_scopes()``) -
  native clients using OAuth2 + PKCE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

from urbanlens.dashboard.models.account.model import ApiKeyScope

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from rest_framework.request import Request
    from rest_framework.views import APIView

#: Scopes a PAT-style ``ApiKey`` may never exercise, only a user-consented OAuth2 client. Direct messages are
#: end-to-end encrypted: reading or sending them requires per-device key material that a long-lived, server-side
#: credential model simply does not have, so a PAT could at best reach ciphertext envelopes it cannot open.
OAUTH2_ONLY_SCOPES = frozenset({ApiKeyScope.MESSAGES_READ, ApiKeyScope.MESSAGES_WRITE})


def credential_grants(credential: object | None, scopes: Iterable[str]) -> bool:
    """Check whether one resolved credential grants every scope in *scopes*.

    The single implementation of the "does this credential allow that?" question, shared by
    :class:`HasApiKeyScope` (the DRF permission every external endpoint runs) and by non-DRF callers
    that resolve a credential by hand - notably ``controllers.media.MediaGateView``, a plain Django
    ``View`` that cannot use a DRF permission class but must apply the exact same rule.

    Args:
        credential: The authenticated credential - a PAT-style ``ApiKey``, a django-oauth-toolkit
        ``AccessToken``, or None for an unauthenticated request.
        scopes: The scopes the caller must hold.

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
    # PAT-style ApiKey.
    if OAUTH2_ONLY_SCOPES & set(required):
        return False
    return required.issubset(set(getattr(credential, "scopes", ())))


@dataclass(frozen=True, slots=True)
class SourceGrants:
    """The outcome of splitting a multi-source payload by what a credential may see.

    Both tuples preserve the declaration order of the mapping that produced them, so a response built by
    iterating ``granted`` is deterministic across requests and across processes (a set would reorder per
    interpreter run, which turns a stable API contract into a flaky one and makes response diffs in
    tests meaningless).
    A client that cannot tell "this section is empty" from "you were not allowed to ask for this
    section" has no way to prompt its user to re-authorize, and will instead render an empty DM tab
    forever.

    Attributes:
        granted: Source keys the credential holds every declared scope for, in declaration order.
        omitted: Source keys that were dropped, in declaration order.
    """

    granted: tuple[str, ...]
    omitted: tuple[str, ...]

    def __contains__(self, key: object) -> bool:
        """Whether *key* survived the filter.

        Args:
            key: The source key being tested.

        Returns:
            True when the credential grants that source.
        """
        return key in self.granted

    def __iter__(self) -> Iterator[str]:
        """Iterate the granted source keys in declaration order.

        Returns:
            An iterator over :attr:`granted`.
        """
        return iter(self.granted)

    def __bool__(self) -> bool:
        """Whether anything at all survived the filter.

        Returns:
            True when at least one source was granted.
        """
        return bool(self.granted)


def filter_sources_by_grants(credential: object | None, mapping: Mapping[str, Iterable[str]]) -> SourceGrants:
    """Split a multi-source endpoint's sections into the ones this credential may see.

    Their required behaviour is *partial fulfilment*: a credential that is scoped for pins but not
    messages gets its pin results and no DM section, rather than a 403 that would make the endpoint
    unusable to every narrowly-scoped integration.

    Args:
        credential: The resolved credential - a PAT-style ``ApiKey``, an OAuth2 ``AccessToken``, or None
        for an unauthenticated request (which grants...
        mapping: ``{source_key: required_scopes}``.

    Returns:
        A: class:`SourceGrants` carrying the granted and omitted keys, both in the mapping's declaration
        order.
    """
    granted: list[str] = []
    omitted: list[str] = []
    for source_key, required in mapping.items():
        # Materialized before the check so a generator value can't be silently
        # consumed-and-empty on a second read by the caller.
        scopes = frozenset(required)
        (granted if credential_grants(credential, scopes) else omitted).append(source_key)
    return SourceGrants(granted=tuple(granted), omitted=tuple(omitted))


class HasApiKeyScope(BasePermission):
    """Requires the authenticating credential to grant every scope in ``view.required_scopes``.

    Views using this must define ``required_scopes`` (an attribute or property yielding a set of
    :class:`~urbanlens.dashboard.models.account.model.ApiKeyScope` values).
    A view with an empty/missing ``required_scopes`` is always denied rather than treated as "no scope
    needed" - an endpoint added here without remembering to set it should fail closed, not open.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Check that ``request.auth`` (ApiKey or OAuth2 AccessToken) grants the view's required scopes."""
        return credential_grants(request.auth, getattr(view, "required_scopes", frozenset()))
