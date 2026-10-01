"""A deactivated account's credentials stop working everywhere at once.

Deactivation is how an account is suspended, and a session already ends with it. An OAuth token or an API key
is a way into the same account, so each entry point that takes one refuses it once its owner is inactive: the
external API, the token endpoint's refresh, code and device grants, token introspection, a socket's connect, and a socket
already open.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import hashlib
from unittest import mock

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_device_grant_model,
    get_grant_model,
    get_refresh_token_model,
)

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import UserNotificationConsumer, _credential_is_still_valid
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware

Application = get_application_model()
AccessToken = get_access_token_model()
RefreshToken = get_refresh_token_model()
Grant = get_grant_model()
DeviceGrant = get_device_grant_model()


def _oauth_token(user: User, suffix: str, *, scope: str = "profile:read") -> tuple[AccessToken, RefreshToken]:
    application = Application.objects.create(
        name=f"Mobile {suffix}",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="urbanlens://oauth/callback",
    )
    access = AccessToken.objects.create(
        user=user,
        application=application,
        token=f"access-{suffix}",
        expires=timezone.now() + timedelta(hours=1),
        scope=scope,
    )
    refresh = RefreshToken.objects.create(
        user=user, application=application, token=f"refresh-{suffix}", access_token=access
    )
    return access, refresh


def _deactivate(user: User) -> None:
    User.objects.filter(pk=user.pk).update(is_active=False)


class OAuthOverHttpTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.access, self.refresh = _oauth_token(self.user, "owner")

    def _whoami(self):
        return self.client.get(reverse("external_api:whoami"), HTTP_AUTHORIZATION=f"Bearer {self.access.token}")

    def test_the_validator_is_this_projects(self) -> None:
        self.assertEqual(
            settings.OAUTH2_PROVIDER.get("OAUTH2_VALIDATOR_CLASS"),
            "urbanlens.dashboard.services.auth.oauth_validator.ActiveOwnerOAuth2Validator",
        )

    def test_an_active_owners_token_is_served(self) -> None:
        self.assertEqual(self._whoami().status_code, 200)

    def test_a_deactivated_owners_token_is_refused(self) -> None:
        _deactivate(self.user)

        self.assertEqual(self._whoami().status_code, 401)

    def test_a_deactivated_owners_refresh_token_is_not_exchanged(self) -> None:
        _deactivate(self.user)

        response = self.client.post(
            reverse("oauth2_provider:token"),
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh.token,
                "client_id": self.refresh.application.client_id,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_a_deactivated_owners_authorization_code_is_not_exchanged(self) -> None:
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        grant = Grant.objects.create(
            user=self.user,
            application=self.access.application,
            code="code-owner",
            expires=timezone.now() + timedelta(minutes=5),
            redirect_uri="urbanlens://oauth/callback",
            scope="profile:read",
            code_challenge=challenge,
            code_challenge_method="S256",
        )
        _deactivate(self.user)

        response = self.client.post(
            reverse("oauth2_provider:token"),
            {
                "grant_type": "authorization_code",
                "code": grant.code,
                "redirect_uri": grant.redirect_uri,
                "client_id": grant.application.client_id,
                "code_verifier": verifier,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def _device_token(self):
        application = Application.objects.create(
            name="Television",
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_DEVICE_CODE,
        )
        device = DeviceGrant.objects.create(
            user=self.user,
            device_code="device-owner",
            user_code="ABCD1234",
            scope="profile:read",
            expires=timezone.now() + timedelta(minutes=10),
            status=DeviceGrant.AUTHORIZED,
            client_id=application.client_id,
        )
        return self.client.post(
            reverse("oauth2_provider:token"),
            {
                "grant_type": Application.GRANT_DEVICE_CODE,
                "device_code": device.device_code,
                "client_id": application.client_id,
            },
        )

    def test_an_active_owners_approved_device_gets_a_token(self) -> None:
        response = self._device_token()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access_token", response.json())

    def test_a_deactivated_owners_approved_device_gets_no_token(self) -> None:
        _deactivate(self.user)

        response = self._device_token()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(AccessToken.objects.filter(user=self.user).exclude(pk=self.access.pk).exists())

    def _introspect(self) -> dict:
        secret = "resource-server-secret"
        server = Application.objects.create(
            name="Resource server",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            client_secret=secret,
        )
        credentials = base64.b64encode(f"{server.client_id}:{secret}".encode()).decode()
        response = self.client.post(
            reverse("oauth2_provider:introspect"),
            {"token": self.access.token},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_introspection_reports_an_active_owners_token_active(self) -> None:
        self.assertTrue(self._introspect()["active"])

    def test_introspection_reports_a_deactivated_owners_token_inactive(self) -> None:
        _deactivate(self.user)

        self.assertEqual(self._introspect(), {"active": False})


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class SocketTests(TransactionTestCase):
    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.access, _refresh = _oauth_token(self.user, "socket", scope="notifications:read")
        self.api_key, _raw = generate_api_key(self.user, "Mobile app")
        ApiKey.objects.filter(pk=self.api_key.pk).update(scopes=[ApiKeyScope.NOTIFICATIONS_READ.value])

    def test_a_deactivated_owners_oauth_token_cannot_open_a_socket(self) -> None:
        _deactivate(self.user)

        async def _test():
            comm = WebsocketCommunicator(
                ApiKeyAuthMiddleware(UserNotificationConsumer.as_asgi()), f"/ws/notifications/?key={self.access.token}"
            )
            comm.scope["user"] = AnonymousUser()
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_an_open_socket_closes_when_its_credentials_owner_is_deactivated(self) -> None:
        self.assertTrue(_credential_is_still_valid(self.access))
        self.assertTrue(_credential_is_still_valid(self.api_key))
        _deactivate(self.user)

        self.assertFalse(_credential_is_still_valid(self.access))
        self.assertFalse(_credential_is_still_valid(self.api_key))


class FixedPricePledgeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _pledge(self, role: SubscriptionRole):
        subscription = baker.make(
            RoleSubscription, user=self.user, role=role, pledged_amount_cents=role.monthly_price_cents
        )
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.update_pledge") as update:
            response = self.client.post(
                reverse("settings.billing.pledge", args=[subscription.pk]), {"amount_dollars": "0.50"}
            )
        return response, update

    def test_a_fixed_price_subscription_cannot_be_repriced(self) -> None:
        response, update = self._pledge(baker.make(SubscriptionRole, pay_what_you_want=False, monthly_price_cents=2000))

        self.assertEqual(response.status_code, 400)
        update.assert_not_called()

    def test_a_pay_what_you_want_pledge_still_changes(self) -> None:
        response, update = self._pledge(baker.make(SubscriptionRole, pay_what_you_want=True, monthly_price_cents=500))

        self.assertEqual(response.status_code, 200)
        update.assert_called_once()
