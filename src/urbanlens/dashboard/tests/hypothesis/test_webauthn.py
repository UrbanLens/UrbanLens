"""Tests for optional passkey (WebAuthn) 2FA.

The actual FIDO2/WebAuthn cryptographic ceremony can't be produced without a
real (or software) authenticator, so tests that exercise
``verify_registration_response``/``verify_authentication_response`` mock
those two py_webauthn entry points - everything around them (challenge
storage/consumption, credential lookup, sign-count/last-used bookkeeping,
the login gate, and ownership checks on the settings endpoints) runs for
real against the database.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker
from webauthn.authentication.verify_authentication_response import VerifiedAuthentication
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import AttestationFormat, AuthenticatorTransport, CredentialDeviceType, PublicKeyCredentialType
from webauthn.registration.verify_registration_response import VerifiedRegistration

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import account as account_controllers
from urbanlens.dashboard.models.account import WebAuthnCredential
from urbanlens.dashboard.services import webauthn as webauthn_service


def _request_with_session(path: str = "/", method: str = "get"):
    """Build a real HttpRequest with a saved, mutable session attached."""
    factory = RequestFactory()
    request = getattr(factory, method)(path)
    SessionMiddleware(lambda _r: None).process_request(request)
    request.session.save()
    return request


def _fake_verified_registration(credential_id: bytes = b"cred-id-123") -> VerifiedRegistration:
    return VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=b"public-key-bytes",
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000000",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"attestation-object-bytes",
        credential_device_type=CredentialDeviceType.MULTI_DEVICE,
        credential_backed_up=True,
    )


class WebAuthnCredentialModelTests(TestCase):
    def test_str_includes_user_id_and_name(self) -> None:
        user: User = baker.make(User, username="alice")
        credential: WebAuthnCredential = baker.make(WebAuthnCredential, user=user, name="Bitwarden")
        self.assertIn(str(user.pk), str(credential))
        self.assertIn("Bitwarden", str(credential))

    def test_str_falls_back_to_pk_when_unnamed(self) -> None:
        credential: WebAuthnCredential = baker.make(WebAuthnCredential, name="")
        self.assertIn(str(credential.pk), str(credential))


class HasPasskeysTests(TestCase):
    def test_false_for_user_with_no_credentials(self) -> None:
        user: User = baker.make(User)
        self.assertFalse(webauthn_service.has_passkeys(user))

    def test_true_once_a_credential_exists(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        self.assertTrue(webauthn_service.has_passkeys(user))

    def test_credentials_are_scoped_per_user(self) -> None:
        user: User = baker.make(User)
        other: User = baker.make(User)
        baker.make(WebAuthnCredential, user=other)
        self.assertFalse(webauthn_service.has_passkeys(user))


class BuildRegistrationOptionsTests(TestCase):
    def test_stores_challenge_in_session(self) -> None:
        user: User = baker.make(User, username="explorer")
        request = _request_with_session()
        webauthn_service.build_registration_options(request, user)
        self.assertIn(webauthn_service.SESSION_REGISTRATION_CHALLENGE, request.session)

    def test_options_reference_the_request_host_as_rp_id(self) -> None:
        user: User = baker.make(User, username="explorer")
        request = _request_with_session()
        options_json = webauthn_service.build_registration_options(request, user)
        self.assertIn('"id": "testserver"', options_json)

    def test_raises_once_max_credentials_reached(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user, _quantity=webauthn_service.MAX_CREDENTIALS_PER_USER)
        request = _request_with_session()
        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.build_registration_options(request, user)


class VerifyAndSaveRegistrationTests(TestCase):
    def test_raises_when_no_registration_pending(self) -> None:
        user: User = baker.make(User)
        request = _request_with_session()
        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_and_save_registration(request, user, "{}", "My key")

    def test_raises_when_verification_fails(self) -> None:
        user: User = baker.make(User)
        request = _request_with_session()
        webauthn_service.build_registration_options(request, user)
        with patch.object(webauthn_service, "verify_registration_response", side_effect=InvalidRegistrationResponse("bad")), self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_and_save_registration(request, user, "{}", "My key")

    def test_successful_verification_creates_credential(self) -> None:
        user: User = baker.make(User)
        request = _request_with_session()
        webauthn_service.build_registration_options(request, user)
        fake_parsed = SimpleNamespace(response=SimpleNamespace(transports=[AuthenticatorTransport.INTERNAL]))

        with (
            patch.object(webauthn_service, "verify_registration_response", return_value=_fake_verified_registration()),
            patch.object(webauthn_service, "parse_registration_credential_json", return_value=fake_parsed),
        ):
            credential = webauthn_service.verify_and_save_registration(request, user, "{}", "  Bitwarden  ")

        self.assertEqual(credential.user_id, user.pk)
        self.assertEqual(credential.name, "Bitwarden")
        self.assertEqual(bytes(credential.credential_id), b"cred-id-123")
        self.assertEqual(credential.transports, ["internal"])
        self.assertTrue(credential.backup_eligible)

    def test_challenge_is_consumed_and_cannot_be_reused(self) -> None:
        user: User = baker.make(User)
        request = _request_with_session()
        webauthn_service.build_registration_options(request, user)
        fake_parsed = SimpleNamespace(response=SimpleNamespace(transports=None))

        with (
            patch.object(webauthn_service, "verify_registration_response", return_value=_fake_verified_registration()),
            patch.object(webauthn_service, "parse_registration_credential_json", return_value=fake_parsed),
        ):
            webauthn_service.verify_and_save_registration(request, user, "{}", "First")

        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_and_save_registration(request, user, "{}", "Second")

    def test_duplicate_credential_id_is_rejected(self) -> None:
        baker.make(WebAuthnCredential, credential_id=b"cred-id-123")
        user: User = baker.make(User)
        request = _request_with_session()
        webauthn_service.build_registration_options(request, user)
        fake_parsed = SimpleNamespace(response=SimpleNamespace(transports=None))

        with (
            patch.object(webauthn_service, "verify_registration_response", return_value=_fake_verified_registration()),
            patch.object(webauthn_service, "parse_registration_credential_json", return_value=fake_parsed),self.assertRaises(webauthn_service.WebAuthnError)
        ):
            webauthn_service.verify_and_save_registration(request, user, "{}", "Dupe")


class BuildAuthenticationOptionsTests(TestCase):
    def test_raises_when_user_has_no_credentials(self) -> None:
        user: User = baker.make(User)
        request = _request_with_session()
        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.build_authentication_options(request, user)

    def test_stores_challenge_when_credentials_exist(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        request = _request_with_session()
        webauthn_service.build_authentication_options(request, user)
        self.assertIn(webauthn_service.SESSION_AUTHENTICATION_CHALLENGE, request.session)


class VerifyAuthenticationTests(TestCase):
    def test_raises_when_no_challenge_pending(self) -> None:
        user: User = baker.make(User)
        request = _request_with_session()
        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_authentication(request, user, "{}")

    def test_raises_on_malformed_json(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        request = _request_with_session()
        webauthn_service.build_authentication_options(request, user)
        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_authentication(request, user, "not json")

    def test_raises_when_credential_not_registered_to_user(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        request = _request_with_session()
        webauthn_service.build_authentication_options(request, user)
        unknown_id = bytes_to_base64url(b"someone-elses-credential")
        with self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_authentication(request, user, f'{{"rawId": "{unknown_id}"}}')

    def test_successful_verification_updates_sign_count_and_last_used(self) -> None:
        user: User = baker.make(User)
        credential: WebAuthnCredential = baker.make(WebAuthnCredential, user=user, credential_id=b"cred-id-123", sign_count=1, last_used_at=None)
        request = _request_with_session()
        webauthn_service.build_authentication_options(request, user)

        raw_id = bytes_to_base64url(b"cred-id-123")
        verified = VerifiedAuthentication(
            credential_id=b"cred-id-123",
            new_sign_count=7,
            credential_device_type=CredentialDeviceType.MULTI_DEVICE,
            credential_backed_up=True,
            user_verified=True,
        )
        with patch.object(webauthn_service, "verify_authentication_response", return_value=verified):
            result = webauthn_service.verify_authentication(request, user, f'{{"rawId": "{raw_id}"}}')

        self.assertEqual(result.pk, credential.pk)
        credential.refresh_from_db()
        self.assertEqual(credential.sign_count, 7)
        self.assertIsNotNone(credential.last_used_at)

    def test_raises_when_verification_fails(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user, credential_id=b"cred-id-123")
        request = _request_with_session()
        webauthn_service.build_authentication_options(request, user)
        raw_id = bytes_to_base64url(b"cred-id-123")
        with patch.object(webauthn_service, "verify_authentication_response", side_effect=InvalidAuthenticationResponse("bad")), self.assertRaises(webauthn_service.WebAuthnError):
            webauthn_service.verify_authentication(request, user, f'{{"rawId": "{raw_id}"}}')


class LoginTwoFactorGateTests(TestCase):
    """CustomLoginView routes accounts with a passkey through the 2FA challenge."""

    def setUp(self) -> None:
        self.user: User = baker.make(User, username="hasnopasskey", is_active=True)
        self.user.set_password("correct horse battery staple")
        self.user.save()

    def test_password_only_account_logs_in_directly(self) -> None:
        response = self.client.post(reverse("login"), {"username": "hasnopasskey", "password": "correct horse battery staple"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("post_login"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_account_with_passkey_is_redirected_to_2fa_and_not_logged_in_yet(self) -> None:
        baker.make(WebAuthnCredential, user=self.user)
        response = self.client.post(reverse("login"), {"username": "hasnopasskey", "password": "correct horse battery staple"})
        self.assertRedirects(response, reverse("login.2fa"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session.get(account_controllers._WEBAUTHN_PENDING_USER_KEY), self.user.pk)


class LoginTwoFactorViewTests(TestCase):
    def test_get_without_pending_challenge_redirects_to_login(self) -> None:
        response = self.client.get(reverse("login.2fa"))
        self.assertRedirects(response, reverse("login"))

    def test_get_with_pending_challenge_renders_page(self) -> None:
        user: User = baker.make(User, username="pending_user", is_active=True)
        session = self.client.session
        session[account_controllers._WEBAUTHN_PENDING_USER_KEY] = user.pk
        session.save()

        response = self.client.get(reverse("login.2fa"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pending_user")

    def test_cancel_clears_pending_session_state(self) -> None:
        user: User = baker.make(User, is_active=True)
        session = self.client.session
        session[account_controllers._WEBAUTHN_PENDING_USER_KEY] = user.pk
        session.save()

        response = self.client.get(reverse("login.2fa.cancel"))
        self.assertRedirects(response, reverse("login"))
        self.assertNotIn(account_controllers._WEBAUTHN_PENDING_USER_KEY, self.client.session)


class LoginTwoFactorVerifyViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User, username="verifyme", is_active=True)
        baker.make(WebAuthnCredential, user=self.user)
        session = self.client.session
        session[account_controllers._WEBAUTHN_PENDING_USER_KEY] = self.user.pk
        session.save()

    def test_without_pending_user_returns_400(self) -> None:
        self.client.session.flush()
        response = self.client.post(reverse("login.2fa.verify"), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_successful_assertion_logs_in_and_clears_pending_state(self) -> None:
        with patch.object(webauthn_service, "verify_authentication", return_value=None):
            response = self.client.post(reverse("login.2fa.verify"), data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertNotIn(account_controllers._WEBAUTHN_PENDING_USER_KEY, self.client.session)

    def test_failed_assertion_does_not_log_in(self) -> None:
        with patch.object(webauthn_service, "verify_authentication", side_effect=webauthn_service.WebAuthnError("nope")):
            response = self.client.post(reverse("login.2fa.verify"), data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)


class PasskeyOwnershipTests(TestCase):
    """Rename/delete are scoped to the requesting user's own credentials."""

    def setUp(self) -> None:
        self.owner: User = baker.make(User, is_active=True)
        self.other: User = baker.make(User, is_active=True)
        self.credential: WebAuthnCredential = baker.make(WebAuthnCredential, user=self.owner, name="Original")

    def test_owner_can_rename_their_own_credential(self) -> None:
        self.client.force_login(self.owner)
        response = self.client.post(reverse("settings.security.passkeys.rename", args=[self.credential.pk]), {"name": "Renamed"})
        self.assertEqual(response.status_code, 302)
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.name, "Renamed")

    def test_other_user_cannot_rename_credential(self) -> None:
        self.client.force_login(self.other)
        response = self.client.post(reverse("settings.security.passkeys.rename", args=[self.credential.pk]), {"name": "Hijacked"})
        self.assertEqual(response.status_code, 404)
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.name, "Original")

    def test_other_user_cannot_delete_credential(self) -> None:
        self.client.force_login(self.other)
        response = self.client.post(reverse("settings.security.passkeys.delete", args=[self.credential.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(WebAuthnCredential.objects.filter(pk=self.credential.pk).exists())

    def test_owner_can_delete_their_own_credential(self) -> None:
        self.client.force_login(self.owner)
        response = self.client.post(reverse("settings.security.passkeys.delete", args=[self.credential.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WebAuthnCredential.objects.filter(pk=self.credential.pk).exists())


class SettingsViewPasskeyContextTests(TestCase):
    def test_settings_page_lists_the_users_passkeys(self) -> None:
        user: User = baker.make(User, is_active=True)
        baker.make(WebAuthnCredential, user=user, name="My YubiKey")
        self.client.force_login(user)
        response = self.client.get(reverse("settings.view"))
        self.assertContains(response, "My YubiKey")
