"""Guards on ``filter_sources_by_grants``, the per-section scope filter.

Global search, the memories timeline, the undo feed and the export bundle each
answer with several domains' data in one response, and each must drop the
sections the calling credential is not scoped for instead of refusing the whole
request. This helper is the single implementation of that split, so the tests
here are written against the ways a *re*-implementation would go wrong rather
than against the happy path:

1. **The OAuth2-only rule survives the indirection.** ``messages:read`` exists
   as an OAuth2-only scope so a leaked PAT cannot be turned into a DM reader,
   and the global-search DM provider returns plaintext excerpts. A PAT-style
   key must not reach that section no matter what else it holds - including
   when its ``scopes`` column has been hand-edited to claim it.
2. **Partial fulfilment, not all-or-nothing.** A credential scoped for one
   section keeps that section and loses only the others.
3. **Fail-closed on an empty declaration.** A section that declares no scopes
   is dropped, not granted - an endpoint computing its requirements
   dynamically must not open up when the computation comes back empty.
4. **Order is the declaration's order**, because these keys drive response
   construction and a set would reorder per process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.external_api.permissions import (
    OAUTH2_ONLY_SCOPES,
    SourceGrants,
    credential_grants,
    filter_sources_by_grants,
)
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

if TYPE_CHECKING:
    from collections.abc import Iterable

_hyp = settings(max_examples=75, deadline=None)

#: Every scope a PAT may legitimately hold. Sampling from this (rather than
#: from the whole vocabulary) keeps the property tests about the *filter* and
#: leaves the credential-kind rule to its own dedicated tests below.
_PAT_SAFE_SCOPES = sorted({scope.value for scope in ApiKeyScope} - {scope.value for scope in OAUTH2_ONLY_SCOPES})


class _FakeApiKey:
    """A PAT-style credential stand-in: a plain ``scopes`` list, no DB row.

    ``credential_grants`` discriminates on the presence of ``allow_scopes``, so
    an object without it is treated exactly as an ``ApiKey`` row is. Using a
    stub keeps these tests on ``SimpleTestCase`` - the filter is pure logic and
    should not need a database to prove it.
    """

    def __init__(self, scopes: Iterable[str]) -> None:
        """Store the scopes this fake credential claims.

        Args:
            scopes: Scope values the credential grants.
        """
        self.scopes = list(scopes)


class _FakeAccessToken:
    """An OAuth2 credential stand-in, discriminated by ``allow_scopes``.

    Mirrors django-oauth-toolkit's ``AccessToken.allow_scopes`` contract: the
    token is already known to be unexpired and unrevoked by the time a
    permission asks it anything, so only the scope subset test remains.
    """

    def __init__(self, scopes: Iterable[str]) -> None:
        """Store the scopes this fake token was issued with.

        Args:
            scopes: Scope values the token grants.
        """
        self.scopes = set(scopes)

    def allow_scopes(self, scopes: list[str]) -> bool:
        """Whether this token covers every scope in *scopes*.

        Args:
            scopes: The scopes being demanded.

        Returns:
            True when the token's grant is a superset.
        """
        return set(scopes).issubset(self.scopes)


#: The DM section of global search, declared the way a caller is expected to
#: declare it: the endpoint's own base scope *plus* what the section needs.
_SEARCH_SECTIONS: dict[str, set[ApiKeyScope]] = {
    "pins": {ApiKeyScope.SEARCH_READ},
    "wikis": {ApiKeyScope.SEARCH_READ, ApiKeyScope.WIKI_READ},
    "messages": {ApiKeyScope.SEARCH_READ, ApiKeyScope.MESSAGES_READ},
}


class SourceGrantsDmLeakTests(SimpleTestCase):
    """The direct-message section must stay unreachable to bearer keys."""

    def test_pat_with_search_read_never_gets_the_messages_section(self) -> None:
        """A bare ``search:read`` key gets pins and nothing that quotes a DM."""
        result = filter_sources_by_grants(_FakeApiKey([ApiKeyScope.SEARCH_READ.value]), _SEARCH_SECTIONS)
        self.assertEqual(result.granted, ("pins",))
        self.assertIn("messages", result.omitted)

    def test_pat_claiming_messages_read_is_still_refused(self) -> None:
        """A hand-edited ``scopes`` column cannot buy a PAT into DM excerpts.

        ``OAUTH2_ONLY_SCOPES`` is a restriction on the credential *kind*, so it
        has to hold even when the row itself claims otherwise - a scope-picker
        bug or a direct UPDATE must not become a DM reader.
        """
        credential = _FakeApiKey(
            [ApiKeyScope.SEARCH_READ.value, ApiKeyScope.MESSAGES_READ.value, ApiKeyScope.WIKI_READ.value]
        )
        result = filter_sources_by_grants(credential, _SEARCH_SECTIONS)
        self.assertNotIn("messages", result.granted)
        self.assertIn("messages", result.omitted)
        # The rest of the response is unaffected: this is a per-section drop,
        # not a refusal of the whole request.
        self.assertEqual(result.granted, ("pins", "wikis"))

    def test_oauth2_token_with_messages_read_does_get_the_section(self) -> None:
        """The scope is not useless - a consented OAuth2 token reaches DMs."""
        credential = _FakeAccessToken([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.MESSAGES_READ.value])
        result = filter_sources_by_grants(credential, _SEARCH_SECTIONS)
        self.assertIn("messages", result.granted)
        self.assertEqual(result.omitted, ("wikis",))

    def test_anonymous_credential_grants_nothing(self) -> None:
        """None is a credential that grants no section at all."""
        result = filter_sources_by_grants(None, _SEARCH_SECTIONS)
        self.assertEqual(result.granted, ())
        self.assertEqual(result.omitted, ("pins", "wikis", "messages"))
        self.assertFalse(result)


class SourceGrantsShapeTests(SimpleTestCase):
    """The return value's contract: ordering, membership, truthiness."""

    def test_declaration_order_is_preserved_in_both_tuples(self) -> None:
        """Response construction iterates ``granted``; the order must be stable."""
        credential = _FakeApiKey([ApiKeyScope.PINS_READ.value, ApiKeyScope.TRIPS_READ.value])
        mapping = {
            "z_pins": {ApiKeyScope.PINS_READ},
            "a_photos": {ApiKeyScope.PHOTOS_READ},
            "m_trips": {ApiKeyScope.TRIPS_READ},
            "b_labels": {ApiKeyScope.LABELS_READ},
        }
        result = filter_sources_by_grants(credential, mapping)
        self.assertEqual(result.granted, ("z_pins", "m_trips"))
        self.assertEqual(result.omitted, ("a_photos", "b_labels"))

    def test_membership_and_iteration_cover_the_granted_keys(self) -> None:
        """``in`` and iteration are the ergonomic path callers will actually use."""
        credential = _FakeApiKey([ApiKeyScope.PINS_READ.value])
        result = filter_sources_by_grants(
            credential, {"pins": {ApiKeyScope.PINS_READ}, "photos": {ApiKeyScope.PHOTOS_READ}}
        )
        self.assertIn("pins", result)
        self.assertNotIn("photos", result)
        self.assertEqual(list(result), ["pins"])
        self.assertTrue(result)

    def test_empty_scope_declaration_fails_closed(self) -> None:
        """A section declaring nothing is dropped, not waved through.

        An endpoint that derives its per-section requirements at runtime and
        produces an empty set has a bug; granting the section would turn that
        bug into a silent disclosure, so it is omitted instead - matching
        ``credential_grants``, which refuses an empty requirement outright.
        """
        credential = _FakeApiKey(list(_PAT_SAFE_SCOPES))
        result = filter_sources_by_grants(credential, {"mystery": set(), "pins": {ApiKeyScope.PINS_READ}})
        self.assertEqual(result.granted, ("pins",))
        self.assertEqual(result.omitted, ("mystery",))

    def test_empty_mapping_is_an_empty_result(self) -> None:
        """No declared sections is not an error, just nothing to serve."""
        result = filter_sources_by_grants(_FakeApiKey([ApiKeyScope.PINS_READ.value]), {})
        self.assertEqual(result, SourceGrants(granted=(), omitted=()))
        self.assertFalse(result)

    def test_generator_values_are_not_silently_consumed(self) -> None:
        """A one-shot iterable value must still be evaluated correctly.

        ``credential_grants`` iterates the scopes it is handed; if the filter
        forwarded a generator unmaterialized and anything read it twice, the
        second read would see an empty set and fail closed on a section the
        caller was entitled to. Cheap to get wrong, invisible in review.
        """
        mapping: dict[str, Any] = {"pins": (scope for scope in [ApiKeyScope.PINS_READ])}
        result = filter_sources_by_grants(_FakeApiKey([ApiKeyScope.PINS_READ.value]), mapping)
        self.assertEqual(result.granted, ("pins",))


class SourceGrantsPropertyTests(SimpleTestCase):
    """Properties that must hold for every credential/mapping combination."""

    @_hyp
    @given(
        held=st.lists(st.sampled_from(_PAT_SAFE_SCOPES), unique=True),
        sections=st.dictionaries(
            st.text(min_size=1, max_size=8),
            st.lists(st.sampled_from(_PAT_SAFE_SCOPES), unique=True).map(frozenset),
            max_size=6,
        ),
    )
    def test_granted_and_omitted_partition_the_mapping(
        self, held: list[str], sections: dict[str, frozenset[str]]
    ) -> None:
        """Every declared key lands in exactly one of the two tuples.

        A key that fell out of both would be a section silently missing from
        the response with nothing telling the client it was dropped.
        """
        result = filter_sources_by_grants(_FakeApiKey(held), sections)
        self.assertEqual(set(result.granted) | set(result.omitted), set(sections))
        self.assertEqual(set(result.granted) & set(result.omitted), set())
        self.assertEqual(len(result.granted) + len(result.omitted), len(sections))

    @_hyp
    @given(
        held=st.lists(st.sampled_from(_PAT_SAFE_SCOPES), unique=True),
        sections=st.dictionaries(
            st.text(min_size=1, max_size=8),
            st.lists(st.sampled_from(_PAT_SAFE_SCOPES), unique=True).map(frozenset),
            max_size=6,
        ),
    )
    def test_a_granted_section_always_agrees_with_credential_grants(
        self, held: list[str], sections: dict[str, frozenset[str]]
    ) -> None:
        """The filter must never be more permissive than the shared scope check.

        This is the whole point of building on ``credential_grants``: if the
        two ever disagree, the disagreement is a section reaching a credential
        that the DRF permission layer would have refused.
        """
        credential = _FakeApiKey(held)
        result = filter_sources_by_grants(credential, sections)
        for key in result.granted:
            self.assertTrue(credential_grants(credential, sections[key]))
        for key in result.omitted:
            self.assertFalse(credential_grants(credential, sections[key]))

    @_hyp
    @given(extra=st.lists(st.sampled_from(_PAT_SAFE_SCOPES), unique=True))
    def test_no_pat_scope_combination_unlocks_the_messages_section(self, extra: list[str]) -> None:
        """Whatever else a bearer key holds, DMs stay out of reach.

        The property form matters here: it is not enough that the one scope
        combination someone thought to test is refused - ``messages:read`` must
        be unreachable across the whole PAT scope space.
        """
        credential = _FakeApiKey([*extra, ApiKeyScope.MESSAGES_READ.value])
        result = filter_sources_by_grants(credential, _SEARCH_SECTIONS)
        self.assertNotIn("messages", result.granted)


class SourceGrantsRealCredentialTests(TestCase):
    """The same rules against real ``ApiKey`` rows, not stand-ins.

    The stubs above encode an assumption about what an ``ApiKey`` looks like to
    ``credential_grants``; this class is what notices if the model ever stops
    matching that assumption (e.g. ``scopes`` becoming a related manager).
    """

    def setUp(self) -> None:
        """Create a user and a key whose scopes each test rewrites."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="keyholder")

    def _key_with_scopes(self, scopes: list[str]) -> ApiKey:
        """Issue a key carrying exactly *scopes*.

        Args:
            scopes: Raw scope values to store on the row.

        Returns:
            The refreshed ``ApiKey`` row.
        """
        api_key, _raw = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        api_key.refresh_from_db()
        return api_key

    def test_real_key_with_search_read_loses_the_messages_section(self) -> None:
        """The stub's behaviour and the row's behaviour are the same."""
        api_key = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value])
        result = filter_sources_by_grants(api_key, _SEARCH_SECTIONS)
        self.assertEqual(result.granted, ("pins",))
        self.assertIn("messages", result.omitted)

    def test_real_key_granted_sections_match_its_scopes(self) -> None:
        """A multi-scope key keeps every section it genuinely covers."""
        api_key = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.WIKI_READ.value])
        result = filter_sources_by_grants(api_key, _SEARCH_SECTIONS)
        self.assertEqual(result.granted, ("pins", "wikis"))
        self.assertEqual(result.omitted, ("messages",))
