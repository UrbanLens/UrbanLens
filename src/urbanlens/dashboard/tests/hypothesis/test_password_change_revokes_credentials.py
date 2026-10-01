"""Changing a password ends every other way into the account that predates it (G3-35, G3-36)."""

from __future__ import annotations

import base64
from datetime import timedelta
import json
import os
from unittest import mock

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from model_bakery import baker
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_grant_model,
    get_refresh_token_model,
)

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.auth.api_keys import authenticate_api_key, generate_api_key

Application = get_application_model()
AccessToken = get_access_token_model()
RefreshToken = get_refresh_token_model()
Grant = get_grant_model()

OLD_PASSWORD = "The Old Password 17!"  # nosec B105 - test fixture password
NEW_PASSWORD = "A Fresh Correct Horse Battery Staple 42!"  # nosec B105 - test fixture password
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _issue_oauth_grant(user: User, suffix: str) -> tuple[str, str]:
    """An application with a live access token, its refresh token, and a pending authorization code.

    Returns:
        ``(access_token, refresh_token)`` strings.
    """
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
        scope="profile:read",
    )
    RefreshToken.objects.create(user=user, application=application, token=f"refresh-{suffix}", access_token=access)
    Grant.objects.create(
        user=user,
        application=application,
        code=f"code-{suffix}",
        expires=timezone.now() + timedelta(minutes=5),
        redirect_uri="urbanlens://oauth/callback",
        scope="profile:read",
    )
    return access.token, f"refresh-{suffix}"


class _CredentialAssertions(TestCase):
    """Shared assertions; holds no tests of its own."""

    def assertOAuthRevoked(self, user: User, refresh: str) -> None:  # noqa: N802 - unittest naming
        self.assertFalse(AccessToken.objects.filter(user=user).exists(), "an access token outlived the password change")
        self.assertFalse(
            RefreshToken.objects.filter(token=refresh, revoked__isnull=True).exists(),
            "a refresh token outlived the password change",
        )
        self.assertFalse(
            Grant.objects.filter(user=user).exists(), "an unredeemed authorization code outlived the password change"
        )

    def assertOAuthIntact(self, user: User) -> None:  # noqa: N802 - unittest naming
        self.assertTrue(AccessToken.objects.filter(user=user).exists())
        self.assertTrue(RefreshToken.objects.filter(user=user, revoked__isnull=True).exists())


class PasswordResetRevokesOAuthTests(_CredentialAssertions):
    """The email-link reset: the victim's usual response to a stolen credential."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(_HIBP_PATCH, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = baker.make(User, email="owner@example.com", is_active=True)
        self.user.set_password(OLD_PASSWORD)
        self.user.save(update_fields=["password"])
        _access, self.refresh = _issue_oauth_grant(self.user, "victim")
        self.stranger = baker.make(User, email="stranger@example.com", is_active=True)
        _issue_oauth_grant(self.stranger, "stranger")

    def _reset(self, **extra: str):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(self.user)]), follow=True
        )
        url = reverse("password_reset_confirm", args=[uidb64, "set-password"])
        return self.client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD, **extra})

    def test_a_stolen_refresh_token_does_not_survive_a_reset(self) -> None:
        response = self._reset()

        self.assertRedirects(response, reverse("password_reset_complete"))
        self.assertOAuthRevoked(self.user, self.refresh)

    def test_the_revoked_refresh_token_cannot_be_exchanged(self) -> None:
        """Behaviour, not rows: the token endpoint itself must refuse it."""
        self._reset()
        application = Application.objects.get(name="Mobile victim")

        response = self.client.post(
            reverse("oauth2_provider:token"),
            {"grant_type": "refresh_token", "refresh_token": self.refresh, "client_id": application.client_id},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_other_accounts_keep_their_tokens(self) -> None:
        self._reset()

        self.assertOAuthIntact(self.stranger)

    def test_a_rejected_reset_revokes_nothing(self) -> None:
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(self.user)]), follow=True
        )
        url = reverse("password_reset_confirm", args=[uidb64, "set-password"])

        self.client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": "something else"})

        self.assertOAuthIntact(self.user)


class InAppPasswordChangeRevokesTests(_CredentialAssertions):
    """The settings page's change-password, and the SSO account's first password."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User, is_active=True)
        self.user.set_password(OLD_PASSWORD)
        self.user.save(update_fields=["password"])
        _access, self.refresh = _issue_oauth_grant(self.user, "victim")
        _key, self.raw_key = generate_api_key(self.user, name="my-integration")
        self.client.force_login(self.user)

    def _change(self, **extra: object):
        payload = {
            "current_secret": OLD_PASSWORD,
            "new_auth_key": _b64(os.urandom(32)),
            "new_auth_salt": _b64(os.urandom(16)),
            **extra,
        }
        return self.client.post(reverse("e2ee.change_password"), json.dumps(payload), content_type="application/json")

    def test_changing_the_password_revokes_oauth_tokens(self) -> None:
        response = self._change()

        self.assertEqual(response.status_code, 200)
        self.assertOAuthRevoked(self.user, self.refresh)

    def test_the_session_that_changed_it_stays_signed_in(self) -> None:
        self._change()

        response = self.client.get(reverse("settings.view"))

        self.assertEqual(response.status_code, 200)

    def test_another_browser_session_is_signed_out(self) -> None:
        other = self.client_class()
        other.force_login(self.user)

        self._change()

        response = other.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 302)

    def test_api_keys_are_kept_unless_the_owner_asks(self) -> None:
        """The same default the reset page offers."""
        response = self._change()

        self.assertIsNotNone(authenticate_api_key(self.raw_key))
        self.assertEqual(response.json()["revoked_api_keys"], 0)

    def test_the_owner_can_revoke_api_keys_with_the_change(self) -> None:
        response = self._change(revoke_api_keys=True)

        self.assertIsNone(authenticate_api_key(self.raw_key))
        self.assertEqual(response.json()["revoked_api_keys"], 1)

    def test_a_wrong_current_password_revokes_nothing(self) -> None:
        response = self._change(current_secret="not it", revoke_api_keys=True)

        self.assertEqual(response.status_code, 403)
        self.assertOAuthIntact(self.user)
        self.assertIsNotNone(authenticate_api_key(self.raw_key))

    def test_an_sso_accounts_first_password_revokes_oauth_tokens(self) -> None:
        """Setting a first password is the only credential step an SSO-only account can take after a theft."""
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("e2ee.change_password"),
            json.dumps({"new_auth_key": _b64(os.urandom(32)), "new_auth_salt": _b64(os.urandom(16))}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertOAuthRevoked(self.user, self.refresh)

    def test_the_settings_page_offers_the_api_key_choice_only_with_keys(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertContains(response, 'id="password-revoke-api-keys"')

        from urbanlens.dashboard.services.auth.api_keys import revoke_all_api_keys

        revoke_all_api_keys(self.user)
        response = self.client.get(reverse("settings.view"))
        self.assertNotContains(response, 'id="password-revoke-api-keys"')


class E2EEEnrollmentReencodeTests(_CredentialAssertions):
    """Enrollment re-derives the stored credential from the same password; nothing about access changed."""

    def test_enrollment_rotation_keeps_delegated_access(self) -> None:
        baker.make(User)
        user = baker.make(User, is_active=True)
        user.set_password(OLD_PASSWORD)
        user.save(update_fields=["password"])
        _issue_oauth_grant(user, "enroll")
        _key, raw_key = generate_api_key(user, name="kept")
        self.client.force_login(user)
        payload = {
            "public_key": _b64(os.urandom(32)),
            "recovery_wrapped_secret": _b64(os.urandom(72)),
            "kdf_opslimit": 2,
            "kdf_memlimit": 67108864,
            "auth_key": _b64(os.urandom(32)),
            "auth_salt": _b64(os.urandom(16)),
            "current_password": OLD_PASSWORD,
        }

        response = self.client.post(reverse("e2ee.enroll"), json.dumps(payload), content_type="application/json")

        self.assertEqual(response.status_code, 201)
        self.assertOAuthIntact(user)
        self.assertIsNotNone(authenticate_api_key(raw_key))
        self.assertEqual(self.client.get(reverse("settings.view")).status_code, 200)


class AdminPasswordChangeRevokesTests(_CredentialAssertions):
    """A staff reset through Django admin is a reset too."""

    def test_admin_password_change_revokes_oauth_tokens(self) -> None:
        admin = baker.make(User, is_staff=True, is_superuser=True, is_active=True)
        target = baker.make(User, is_active=True)
        _access, refresh = _issue_oauth_grant(target, "admin")
        _key, raw_key = generate_api_key(target, name="kept-by-admin")
        self.client.force_login(admin)

        with mock.patch(_HIBP_PATCH, return_value=False):
            response = self.client.post(
                reverse("admin:auth_user_password_change", args=[target.pk]),
                {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD, "usable_password": "true"},
            )

        self.assertEqual(
            response.status_code, 302, getattr(response, "context", None) and response.context.get("form").errors
        )
        target.refresh_from_db()
        self.assertTrue(target.check_password(NEW_PASSWORD))
        self.assertOAuthRevoked(target, refresh)
        self.assertIsNotNone(authenticate_api_key(raw_key))

    def test_admin_can_revoke_api_keys_with_the_change(self) -> None:
        admin = baker.make(User, is_staff=True, is_superuser=True, is_active=True)
        target = baker.make(User, is_active=True)
        _key, raw_key = generate_api_key(target, name="revoked-by-admin")
        self.client.force_login(admin)

        with mock.patch(_HIBP_PATCH, return_value=False):
            self.client.post(
                reverse("admin:auth_user_password_change", args=[target.pk]),
                {
                    "password1": NEW_PASSWORD,
                    "password2": NEW_PASSWORD,
                    "usable_password": "true",
                    "revoke_api_keys": "on",
                },
            )

        self.assertIsNone(authenticate_api_key(raw_key))


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class SessionSocketClosesOnPasswordChangeTests(TransactionTestCase):
    """A socket opened by a session the password change ended must not keep delivering."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User, is_active=True)
        self.user.set_password(OLD_PASSWORD)
        self.user.save(update_fields=["password"])

    def _communicator(self, user: User) -> WebsocketCommunicator:
        from urbanlens.dashboard.consumers import UserNotificationConsumer
        from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware

        comm = WebsocketCommunicator(ApiKeyAuthMiddleware(UserNotificationConsumer.as_asgi()), "/ws/notifications/")
        comm.scope["url_route"] = {"kwargs": {}}
        comm.scope["user"] = user
        return comm

    @mock.patch("urbanlens.dashboard.consumers._CREDENTIAL_REVALIDATION_INTERVAL_SECONDS", 0.05)
    def test_a_session_socket_closes_after_the_password_changes(self) -> None:
        from urbanlens.dashboard.consumers import CREDENTIALS_CHANGED_CLOSE_CODE

        connected_as = User.objects.get(pk=self.user.pk)

        async def _test():
            comm = self._communicator(connected_as)
            connected, _ = await comm.connect()
            self.assertTrue(connected)

            def change() -> None:
                fresh = User.objects.get(pk=self.user.pk)
                fresh.set_password(NEW_PASSWORD)
                fresh.save(update_fields=["password"])

            await database_sync_to_async(change)()

            message = await comm.receive_output(timeout=5)
            self.assertEqual(message["type"], "websocket.close")
            # Not 4404: the tab that made the change holds a fresh session and should reconnect with it.
            self.assertEqual(message["code"], CREDENTIALS_CHANGED_CLOSE_CODE)
            self.assertNotEqual(CREDENTIALS_CHANGED_CLOSE_CODE, 4404)

        _run(_test())

    @mock.patch("urbanlens.dashboard.consumers._CREDENTIAL_REVALIDATION_INTERVAL_SECONDS", 0.05)
    def test_a_session_socket_stays_open_while_the_password_is_unchanged(self) -> None:
        """Negative control for the test above."""
        connected_as = User.objects.get(pk=self.user.pk)

        async def _test():
            comm = self._communicator(connected_as)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            self.assertTrue(await comm.receive_nothing(timeout=0.4))
            await comm.disconnect()

        _run(_test())


class SafetyChatSessionClosesOnPasswordChangeTests(TransactionTestCase):
    """The safety check-in chat runs its own revalidation loop; it must end a stale session too."""

    def setUp(self) -> None:
        baker.make(User)
        self.owner = baker.make(User, is_active=True)
        self.owner.set_password(OLD_PASSWORD)
        self.owner.save(update_fields=["password"])
        self.checkin = baker.make("dashboard.SafetyCheckin", profile=self.owner.profile)

    def _owner_route(self, user: User) -> WebsocketCommunicator:
        from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer
        from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware

        comm = WebsocketCommunicator(
            ApiKeyAuthMiddleware(SafetyCheckinChatConsumer.as_asgi()), f"/ws/safety/checkin/{self.checkin.uuid}/chat/"
        )
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = user
        return comm

    @mock.patch("urbanlens.dashboard.consumers._PARTNER_REVALIDATION_INTERVAL_SECONDS", 0.05)
    def test_the_owners_session_socket_closes_after_the_password_changes(self) -> None:
        connected_as = User.objects.get(pk=self.owner.pk)

        async def _test():
            comm = self._owner_route(connected_as)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            self.assertTrue(await comm.receive_nothing(timeout=0.3), "closed before anything changed")

            def change() -> None:
                fresh = User.objects.get(pk=self.owner.pk)
                fresh.set_password(NEW_PASSWORD)
                fresh.save(update_fields=["password"])

            await database_sync_to_async(change)()

            message = await comm.receive_output(timeout=5)
            self.assertEqual(message["type"], "websocket.close")
            self.assertEqual(message["code"], 4401)

        _run(_test())
