"""OAuth clients are provisioned by the site, not registered by accounts, and every token belongs to an account.

django-oauth-toolkit mounts its application management pages beside the token endpoints. Left open, any account
could register a client of any grant type: a password-grant client trades a username and password for tokens
without the second factor sign-in would ask for, and a client-credentials token belongs to no account at all.
"""

from __future__ import annotations

import base64
from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model, get_application_model

from urbanlens.core.tests.testcase import TestCase

Application = get_application_model()
AccessToken = get_access_token_model()


class ClientRegistrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_an_account_cannot_register_a_client(self) -> None:
        response = self.client.post(
            "/oauth/applications/register/",
            {
                "name": "Mine",
                "client_type": Application.CLIENT_CONFIDENTIAL,
                "authorization_grant_type": Application.GRANT_PASSWORD,
                "client_id": "self-registered",
                "client_secret": "self-registered-secret",
                "redirect_uris": "",
                "algorithm": "",
            },
        )

        self.assertFalse(Application.objects.filter(client_id="self-registered").exists(), response.get("Location"))
        self.assertEqual(response.status_code, 404)

    def test_no_application_management_page_is_mounted(self) -> None:
        for name in ("list", "register"):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(f"oauth2_provider:{name}")

    def test_an_account_can_still_see_and_disconnect_its_connected_apps(self) -> None:
        self.assertEqual(self.client.get(reverse("oauth2_provider:authorized-token-list")).status_code, 200)


class RefusedGrantTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = User.objects.create_user(username="walker", password="correct horse battery staple")

    def _client(self, grant_type: str) -> tuple[Application, str]:
        secret = f"{grant_type}-secret"
        return Application.objects.create(
            name=grant_type,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=grant_type,
            client_secret=secret,
        ), secret

    def _token(self, client: Application, secret: str, **data: str):
        return self.client.post(
            reverse("oauth2_provider:token"), {"client_id": client.client_id, "client_secret": secret, **data}
        )

    def test_a_password_grant_client_gets_no_token(self) -> None:
        client, secret = self._client(Application.GRANT_PASSWORD)

        response = self._token(
            client, secret, grant_type="password", username="walker", password="correct horse battery staple"
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access_token", response.json())

    def test_a_client_credentials_client_gets_no_token(self) -> None:
        client, secret = self._client(Application.GRANT_CLIENT_CREDENTIALS)

        response = self._token(client, secret, grant_type="client_credentials", scope="media:read")

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access_token", response.json())

    def test_a_token_that_belongs_to_no_account_opens_nothing(self) -> None:
        client, secret = self._client(Application.GRANT_CLIENT_CREDENTIALS)
        token = AccessToken.objects.create(
            user=None,
            application=client,
            token="ownerless",
            expires=timezone.now() + timedelta(hours=1),
            scope="profile:read media:read",
        )
        bearer = f"Bearer {token.token}"

        self.assertEqual(self.client.get(reverse("external_api:whoami"), HTTP_AUTHORIZATION=bearer).status_code, 401)
        self.assertIn(self.client.get("/media/pins/none.jpg", HTTP_AUTHORIZATION=bearer).status_code, {401, 403, 404})
        self.assertEqual(
            self.client.post(
                reverse("oauth2_provider:introspect"),
                {"token": token.token},
                HTTP_AUTHORIZATION=f"Basic {base64.b64encode(f'{client.client_id}:{secret}'.encode()).decode()}",
            ).json(),
            {"active": False},
        )
