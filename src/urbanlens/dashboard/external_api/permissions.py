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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

from urbanlens.dashboard.models.account.model import ApiKeyScope

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

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


@dataclass(frozen=True, slots=True)
class SourceGrants:
    """The outcome of splitting a multi-source payload by what a credential may see.

    Returned by :func:`filter_sources_by_grants`. Both tuples preserve the
    declaration order of the mapping that produced them, so a response built by
    iterating ``granted`` is deterministic across requests and across processes
    (a set would reorder per interpreter run, which turns a stable API contract
    into a flaky one and makes response diffs in tests meaningless).

    ``omitted`` exists because these endpoints answer 200 while silently
    dropping sections. A client that cannot tell "this section is empty" from
    "you were not allowed to ask for this section" has no way to prompt its user
    to re-authorize, and will instead render an empty DM tab forever. Callers
    are expected to surface the omitted keys in the response body - naming the
    dropped sections is not a leak, since the client already knows which scopes
    its own credential holds.

    Attributes:
        granted: Source keys the credential holds every declared scope for, in
            declaration order.
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

    Several endpoints aggregate data from more than one domain in a single
    response - global search (pins, wikis, photos, direct messages), the
    memories timeline, the undo feed, the export bundle. Their required
    behaviour is *partial fulfilment*: a credential that is scoped for pins but
    not messages gets its pin results and no DM section, rather than a 403 that
    would make the endpoint unusable to every narrowly-scoped integration.

    That per-section decision is the reason this helper exists rather than each
    endpoint writing its own ``credential_grants`` branch. Four hand-rolled
    copies of a scope filter is four chances for one of them to test the wrong
    scope, to check with ``any()`` where the others use ``all()``, or to treat
    "no scopes declared for this section" as "no scope needed" - and a filter
    that is wrong in the permissive direction fails silently, because the
    response still looks right to whoever is testing it with an all-scopes key.
    Exactly the drift this module's docstring is written against.

    **The case that must not regress.** The global-search direct-message
    provider returns plaintext excerpts of message bodies.
    :data:`OAUTH2_ONLY_SCOPES` makes ``messages:read`` unreachable to PAT-style
    keys precisely so that a bearer key leaked into a CI log or a screenshot
    cannot be turned into a DM reader. Declaring the DM section as
    ``{"messages": {SEARCH_READ, MESSAGES_READ}}`` and routing it through here
    means that rule is applied by :func:`credential_grants` - the one function
    that knows about credential kinds - instead of being re-derived by a search
    endpoint whose author is thinking about relevance ranking. A bare
    ``search:read`` can never reach that section, no matter what the endpoint
    does with the result.

    Args:
        credential: The resolved credential - a PAT-style ``ApiKey``, an OAuth2
            ``AccessToken``, or None for an unauthenticated request (which
            grants nothing at all).
        mapping: ``{source_key: required_scopes}``. Each value must list
            **every** scope that section needs, including the endpoint's own
            base scope - this helper deliberately does not assume the view's
            ``required_scopes`` were already satisfied, so a section can never
            be granted on the strength of a check that happened somewhere else.
            A section declaring an empty scope set is *omitted*, not granted:
            an endpoint that computes its requirements dynamically and comes up
            empty must fail closed, matching :func:`credential_grants`.

    Returns:
        A :class:`SourceGrants` carrying the granted and omitted keys, both in
        the mapping's declaration order.
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
