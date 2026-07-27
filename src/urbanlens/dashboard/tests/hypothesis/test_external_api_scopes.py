"""Guards on the external API's scope vocabulary and who may hold each scope.

Three separate invariants, all of which are the kind that break silently:

1. ``ApiKeyScope`` and ``OAUTH2_PROVIDER["SCOPES"]`` must stay identical.
   They are duplicated because settings load before the app registry and so
   cannot import a model - nothing but a test can catch them drifting.
2. ``_default_api_key_scopes()`` must keep returning exactly its original four
   values. Adding the newer scopes there would retroactively widen every
   already-issued key's grant to cover messages, photos and safety check-ins
   that the key's owner never consented to.
3. The ``messages:*`` scopes must be unreachable by a PAT-style key even if one
   somehow carries them.

Most of this needs no database, so it lives in ``SimpleTestCase`` classes that
run without Postgres.
"""

from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase

from urbanlens.dashboard.external_api.permissions import OAUTH2_ONLY_SCOPES, HasApiKeyScope
from urbanlens.dashboard.models.account.model import ApiKeyScope, _default_api_key_scopes

#: The grant every API key has had since the feature shipped. Spelled out as
#: literals rather than referencing ApiKeyScope members, so that renaming or
#: repointing a member cannot quietly change what this asserts.
ORIGINAL_DEFAULT_SCOPES = ["profile:read", "pins:read", "pins:write", "push:manage"]


class _FakeApiKey:
    """Stands in for a PAT-style ``ApiKey`` - no ``allow_scopes``, a ``scopes`` list."""

    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes


class _FakeAccessToken:
    """Stands in for an OAuth2 ``AccessToken`` - has ``allow_scopes``."""

    def __init__(self, scopes: list[str]) -> None:
        self.granted = set(scopes)

    def allow_scopes(self, scopes: list[str]) -> bool:
        """Mirror ``AbstractAccessToken.allow_scopes``."""
        return set(scopes).issubset(self.granted)


class _FakeView:
    """Minimal stand-in for a view exposing ``required_scopes``."""

    def __init__(self, required_scopes: frozenset) -> None:
        self.required_scopes = required_scopes


class _FakeRequest:
    """Minimal stand-in for a request carrying an authenticated credential."""

    def __init__(self, auth: object) -> None:
        self.auth = auth


class ScopeVocabularyTests(SimpleTestCase):
    """ApiKeyScope is the single source of truth; settings must mirror it verbatim."""

    def test_settings_scopes_match_enum_exactly(self) -> None:
        """Every value and label in OAUTH2_PROVIDER["SCOPES"] matches the enum."""
        expected = {scope.value: scope.label for scope in ApiKeyScope}
        self.assertEqual(settings.OAUTH2_PROVIDER["SCOPES"], expected)

    def test_no_extra_scopes_in_settings(self) -> None:
        """Settings declares no scope the enum doesn't know about."""
        self.assertEqual(set(settings.OAUTH2_PROVIDER["SCOPES"]), {scope.value for scope in ApiKeyScope})

    def test_scope_values_are_domain_shaped(self) -> None:
        """Every scope is `<domain>:<action>` - the naming convention siblings rely on."""
        for scope in ApiKeyScope:
            with self.subTest(scope=scope.value):
                domain, _, action = scope.value.partition(":")
                self.assertTrue(domain, f"{scope.value} has no domain part")
                self.assertIn(action, {"read", "write", "manage"})

    def test_scope_values_are_unique(self) -> None:
        """No two members share a value, which would make a grant ambiguous."""
        values = [scope.value for scope in ApiKeyScope]
        self.assertEqual(len(values), len(set(values)))


class DefaultScopeImmutabilityTests(SimpleTestCase):
    """The default PAT grant must not widen when the vocabulary grows.

    This is a deliberate security decision, not an oversight: see
    ``_default_api_key_scopes``'s docstring. If a scope-picker UI later makes
    per-key grants selectable, *that* is when this list may change - and this
    test should be updated as part of the same, intentional change.
    """

    def test_default_scopes_are_exactly_the_original_four(self) -> None:
        """No new scope has been added to the implicit grant every key receives."""
        self.assertEqual(_default_api_key_scopes(), ORIGINAL_DEFAULT_SCOPES)

    def test_oauth2_default_scopes_are_exactly_the_original_four(self) -> None:
        """A token that requested nothing specific gets the same minimal grant."""
        self.assertEqual(settings.OAUTH2_PROVIDER["DEFAULT_SCOPES"], ORIGINAL_DEFAULT_SCOPES)

    def test_defaults_exclude_every_sensitive_scope(self) -> None:
        """Messages, safety, photos and friends are never granted by default."""
        granted = set(_default_api_key_scopes())
        for scope in (
            ApiKeyScope.MESSAGES_READ,
            ApiKeyScope.MESSAGES_WRITE,
            ApiKeyScope.SAFETY_READ,
            ApiKeyScope.SAFETY_WRITE,
            ApiKeyScope.PHOTOS_READ,
            ApiKeyScope.PHOTOS_WRITE,
            ApiKeyScope.SOCIAL_READ,
            ApiKeyScope.SOCIAL_WRITE,
            ApiKeyScope.SETTINGS_WRITE,
        ):
            with self.subTest(scope=scope.value):
                self.assertNotIn(scope.value, granted)


class Oauth2OnlyScopeTests(SimpleTestCase):
    """The messages scopes are reachable only by a real OAuth2 credential."""

    def setUp(self) -> None:
        self.permission = HasApiKeyScope()

    def test_oauth2_only_scopes_are_the_message_scopes(self) -> None:
        """The frozenset holds exactly the two messages scopes."""
        self.assertEqual(OAUTH2_ONLY_SCOPES, frozenset({ApiKeyScope.MESSAGES_READ, ApiKeyScope.MESSAGES_WRITE}))

    def test_api_key_holding_messages_read_is_still_refused(self) -> None:
        """Even a key whose row grants messages:read cannot use it."""
        credential = _FakeApiKey([ApiKeyScope.MESSAGES_READ.value])
        view = _FakeView(frozenset({ApiKeyScope.MESSAGES_READ}))
        self.assertFalse(self.permission.has_permission(_FakeRequest(credential), view))

    def test_api_key_holding_messages_write_is_still_refused(self) -> None:
        """Same for the write half of the pair."""
        credential = _FakeApiKey([ApiKeyScope.MESSAGES_WRITE.value])
        view = _FakeView(frozenset({ApiKeyScope.MESSAGES_WRITE}))
        self.assertFalse(self.permission.has_permission(_FakeRequest(credential), view))

    def test_api_key_refused_when_messages_scope_is_one_of_several_required(self) -> None:
        """A messages scope anywhere in the requirement poisons the whole check."""
        credential = _FakeApiKey([ApiKeyScope.PINS_READ.value, ApiKeyScope.MESSAGES_READ.value])
        view = _FakeView(frozenset({ApiKeyScope.PINS_READ, ApiKeyScope.MESSAGES_READ}))
        self.assertFalse(self.permission.has_permission(_FakeRequest(credential), view))

    def test_oauth2_token_may_hold_messages_read(self) -> None:
        """The restriction is on credential kind - a consented token is fine."""
        credential = _FakeAccessToken([ApiKeyScope.MESSAGES_READ.value])
        view = _FakeView(frozenset({ApiKeyScope.MESSAGES_READ}))
        self.assertTrue(self.permission.has_permission(_FakeRequest(credential), view))

    def test_api_key_still_works_for_ordinary_scopes(self) -> None:
        """The new gate doesn't break the non-messages case."""
        credential = _FakeApiKey([ApiKeyScope.PINS_READ.value])
        view = _FakeView(frozenset({ApiKeyScope.PINS_READ}))
        self.assertTrue(self.permission.has_permission(_FakeRequest(credential), view))

    def test_missing_required_scopes_fails_closed(self) -> None:
        """A view that forgot to declare its scopes is denied, not allowed."""
        credential = _FakeApiKey([ApiKeyScope.PINS_READ.value])
        self.assertFalse(self.permission.has_permission(_FakeRequest(credential), _FakeView(frozenset())))

    def test_unauthenticated_request_is_denied(self) -> None:
        """No credential means no access, regardless of what the view wants."""
        view = _FakeView(frozenset({ApiKeyScope.PINS_READ}))
        self.assertFalse(self.permission.has_permission(_FakeRequest(None), view))
