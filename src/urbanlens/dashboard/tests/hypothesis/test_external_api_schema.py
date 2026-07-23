"""Tests for the external API's OpenAPI schema and OAuth2 token authentication.

The schema is the published contract native clients generate code from, so it
must (a) exist, (b) cover only the external surface - never the internal
HTMX/REST endpoints - and (c) never silently drift from what the sync
service actually emits. OAuth2 bearer tokens (django-oauth-toolkit) are the
native apps' credential and must be honored by the same views, under the
same scope rules, as PAT-style API keys.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model, get_application_model

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers import SyncPinSerializer
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.pin_sync import sync_pins_page

Application = get_application_model()
AccessToken = get_access_token_model()


class SchemaEndpointTests(TestCase):
    """The schema endpoint serves the external surface and nothing else."""

    def test_schema_is_served_without_authentication(self) -> None:
        response = self.client.get(reverse("external_api:schema"))
        self.assertEqual(response.status_code, 200)

    def test_schema_covers_external_endpoints_only(self) -> None:
        body = self.client.get(reverse("external_api:schema")).content.decode()
        self.assertIn("/dashboard/api/external/v1/pins/", body)
        self.assertIn("/dashboard/api/external/v1/pins/deleted/", body)
        self.assertIn("/dashboard/api/external/v1/whoami/", body)
        # The internal surfaces must never leak into the published contract.
        self.assertNotIn("/dashboard/rest/", body)


class SyncPayloadContractTests(TestCase):
    """SyncPinSerializer (schema-only) must exactly match the real sync payload."""

    def test_schema_serializer_fields_match_the_served_payload(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        user = baker.make(User)
        profile = Profile.objects.get(user=user)
        create_pin_for_profile(profile, name="Contract", latitude=42.5, longitude=-73.5)
        page = sync_pins_page(profile)
        (payload,) = page.pins
        self.assertEqual(set(payload), set(SyncPinSerializer().fields), "SyncPinSerializer and services.pin_sync._serialize_sync_pin have drifted apart - update both together.")


class OAuth2TokenAuthTests(TestCase):
    """OAuth2 access tokens authenticate the external API under the same scope rules."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.application = Application.objects.create(
            name="UrbanLens Mobile",
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="urbanlens://oauth/callback",
        )
        self.url = reverse("external_api:pins")

    def _token(self, scope: str, *, expires_in_hours: int = 1) -> str:
        token = AccessToken.objects.create(
            user=self.user,
            application=self.application,
            token=f"tok-{scope.replace(' ', '-')}-{expires_in_hours}",
            expires=timezone.now() + timedelta(hours=expires_in_hours),
            scope=scope,
        )
        return token.token

    def _get(self, token: str):
        return self.client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_token_with_pins_read_scope_can_sync(self) -> None:
        create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5)
        response = self._get(self._token("pins:read"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["pins"]), 1)

    def test_token_missing_the_required_scope_is_forbidden(self) -> None:
        response = self._get(self._token("profile:read"))
        self.assertEqual(response.status_code, 403)

    def test_expired_token_is_rejected(self) -> None:
        response = self._get(self._token("pins:read", expires_in_hours=-1))
        self.assertEqual(response.status_code, 401)

    def test_token_scopes_gate_writes_independently_of_reads(self) -> None:
        token = self._token("pins:read")
        response = self.client.post(
            self.url,
            data={"latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 403)

    def test_write_scoped_token_can_create_pins(self) -> None:
        token = self._token("pins:write")
        response = self.client.post(
            self.url,
            data={"name": "Token Pin", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 201, response.content)
