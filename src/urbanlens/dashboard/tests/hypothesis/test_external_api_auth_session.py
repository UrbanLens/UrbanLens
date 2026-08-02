"""Tests for the external API's credential-introspection endpoint.

``auth/session/`` is the single deliberate exception to the fail-closed
``HasApiKeyScope`` default, so these tests pin down both halves of that: it must
still require *authentication* (the exception is about scopes, not about being
open), and it must describe either credential kind accurately enough for a
client to hide unreachable UI and refresh before expiry.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model, get_application_model

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.views import AuthSessionView, UnscopedExternalApiView
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.oauth_clients import FIRST_PARTY_CLIENT_ID
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

AccessToken = get_access_token_model()
Application = get_application_model()


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class AuthSessionApiKeyTests(TestCase):
    """The endpoint describes a PAT-style ApiKey correctly."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.key, self.raw_key = generate_api_key(self.user, "My Integration")
        self.url = reverse("external_api:auth.session")

    def test_reports_api_key_credential_type(self) -> None:
        response = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["credential_type"], "api_key")

    def test_reports_the_keys_scopes_sorted(self) -> None:
        response = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(response.json()["scopes"], sorted(self.key.scopes))

    def test_api_key_has_no_expiry_or_client_id(self) -> None:
        """Keys are revoked rather than expiring, and belong to no OAuth2 client."""
        payload = self.client.get(self.url, **_bearer(self.raw_key)).json()
        self.assertIsNone(payload["expires_at"])
        self.assertIsNone(payload["client_id"])

    def test_reports_the_keys_label_and_owner(self) -> None:
        payload = self.client.get(self.url, **_bearer(self.raw_key)).json()
        self.assertEqual(payload["name"], "My Integration")
        self.assertEqual(payload["user_uuid"], str(self.profile.uuid))
        self.assertIsNotNone(payload["issued_at"])

    def test_works_without_any_particular_scope(self) -> None:
        """The whole point: a credential can always describe itself."""
        ApiKey.objects.filter(pk=self.key.pk).update(scopes=[])
        self.assertEqual(self.client.get(self.url, **_bearer(self.raw_key)).status_code, 200)

    def test_still_requires_authentication(self) -> None:
        """Scope-free is not auth-free."""
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_revoked_key_is_rejected(self) -> None:
        ApiKey.objects.filter(pk=self.key.pk).update(revoked_at=timezone.now())
        self.assertIn(self.client.get(self.url, **_bearer(self.raw_key)).status_code, (401, 403))

    def test_reported_scopes_gate_what_the_client_may_try(self) -> None:
        """A narrowed key reports the narrowed grant, not the issuing default."""
        ApiKey.objects.filter(pk=self.key.pk).update(scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self.url, **_bearer(self.raw_key)).json()["scopes"], [ApiKeyScope.PINS_READ.value])


class AuthSessionOauth2Tests(TestCase):
    """The endpoint describes an OAuth2 access token correctly."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        # Provisioned by the 0013 data migration during test DB setup.
        self.application = Application.objects.get(client_id=FIRST_PARTY_CLIENT_ID)
        self.expires = timezone.now() + timedelta(hours=1)
        self.token = AccessToken.objects.create(
            user=self.user,
            application=self.application,
            token="test-access-token-value",
            expires=self.expires,
            scope=f"{ApiKeyScope.PINS_READ.value} {ApiKeyScope.PROFILE_READ.value}",
        )
        self.url = reverse("external_api:auth.session")

    def test_reports_oauth2_credential_type(self) -> None:
        response = self.client.get(self.url, **_bearer(self.token.token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["credential_type"], "oauth2")

    def test_reports_granted_scopes_sorted(self) -> None:
        """The list comes from the raw scope string - AccessToken.scopes is a dict."""
        payload = self.client.get(self.url, **_bearer(self.token.token)).json()
        self.assertEqual(payload["scopes"], sorted([ApiKeyScope.PINS_READ.value, ApiKeyScope.PROFILE_READ.value]))

    def test_reports_expiry_and_client_identity(self) -> None:
        payload = self.client.get(self.url, **_bearer(self.token.token)).json()
        self.assertIsNotNone(payload["expires_at"])
        self.assertEqual(payload["client_id"], FIRST_PARTY_CLIENT_ID)
        self.assertEqual(payload["name"], self.application.name)
        self.assertEqual(payload["user_uuid"], str(self.profile.uuid))

    def test_expired_token_is_rejected(self) -> None:
        AccessToken.objects.filter(pk=self.token.pk).update(expires=timezone.now() - timedelta(hours=1))
        self.assertIn(self.client.get(self.url, **_bearer(self.token.token)).status_code, (401, 403))


class UnscopedViewContractTests(TestCase):
    """The scope-free base stays narrowly scoped in what it exempts."""

    def test_auth_session_is_the_only_unscoped_view(self) -> None:
        """A new endpoint inheriting this base would bypass scope checks entirely."""
        from urbanlens.dashboard.external_api import views

        unscoped = {
            name
            for name in dir(views)
            if isinstance(getattr(views, name), type) and issubclass(getattr(views, name), UnscopedExternalApiView) and getattr(views, name) is not UnscopedExternalApiView
        }
        self.assertEqual(unscoped, {"AuthSessionView"})

    def test_auth_session_is_classified_as_a_read(self) -> None:
        """It declares no scopes, so it needs the explicit tier override."""
        self.assertEqual(AuthSessionView.throttle_tier_by_method.get("GET"), "read")
