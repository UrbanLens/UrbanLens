"""Tests for TOTP (authenticator app) and backup-code 2FA fallback.

Unlike WebAuthn, TOTP is a deterministic algorithm (RFC 6238) - these tests
generate real codes with pyotp and verify them for real against the service
layer, no mocking needed anywhere in this file.
"""

from __future__ import annotations

import time

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pyotp

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import account as account_controllers
from urbanlens.dashboard.models.account import BackupCode, TOTPDevice, WebAuthnCredential
from urbanlens.dashboard.services import two_factor


class HasSecondFactorTests(TestCase):
    def test_false_with_no_factors(self) -> None:
        user: User = baker.make(User)
        self.assertFalse(two_factor.has_second_factor(user))

    def test_true_with_passkey_only(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        self.assertTrue(two_factor.has_second_factor(user))

    def test_true_with_totp_only(self) -> None:
        user: User = baker.make(User)
        baker.make(TOTPDevice, user=user)
        self.assertTrue(two_factor.has_second_factor(user))

    def test_true_with_both(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        baker.make(TOTPDevice, user=user)
        self.assertTrue(two_factor.has_second_factor(user))


class MaybeClearBackupCodesTests(TestCase):
    def test_clears_codes_once_last_factor_removed(self) -> None:
        user: User = baker.make(User)
        credential = baker.make(WebAuthnCredential, user=user)
        baker.make(BackupCode, user=user, _quantity=3)
        credential.delete()  # simulate the last factor being gone

        two_factor.maybe_clear_backup_codes(user)
        self.assertEqual(BackupCode.objects.filter(user=user).count(), 0)

    def test_keeps_codes_when_a_factor_remains(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        baker.make(TOTPDevice, user=user)
        baker.make(BackupCode, user=user, _quantity=3)

        two_factor.maybe_clear_backup_codes(user)
        self.assertEqual(BackupCode.objects.filter(user=user).count(), 3)


class TOTPEnrollmentTests(TestCase):
    def test_setup_code_verification_succeeds_with_real_code(self) -> None:
        secret = two_factor.generate_totp_secret()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(two_factor.verify_totp_setup_code(secret, code))

    def test_setup_code_verification_fails_with_wrong_code(self) -> None:
        secret = two_factor.generate_totp_secret()
        self.assertFalse(two_factor.verify_totp_setup_code(secret, "000000"))

    def test_setup_code_with_a_middle_space_still_verifies(self) -> None:
        """Some authenticator apps display/copy codes as "123 456" - must still match."""
        secret = two_factor.generate_totp_secret()
        code = pyotp.TOTP(secret).now()
        spaced = f"{code[:3]} {code[3:]}"
        self.assertTrue(two_factor.verify_totp_setup_code(secret, spaced))

    def test_enroll_persists_a_totp_device(self) -> None:
        user: User = baker.make(User)
        secret = two_factor.generate_totp_secret()
        device = two_factor.enroll_totp(user, secret)
        device.refresh_from_db()
        self.assertEqual(device.secret, secret)
        self.assertTrue(two_factor.has_totp(user))

    def test_provisioning_uri_includes_issuer_and_username(self) -> None:
        user: User = baker.make(User, username="urbexer", email="")
        secret = two_factor.generate_totp_secret()
        uri = two_factor.totp_provisioning_uri(user, secret)
        self.assertIn("UrbanLens", uri)
        self.assertIn("urbexer", uri)

    def test_disable_removes_device_and_clears_orphaned_backup_codes(self) -> None:
        user: User = baker.make(User)
        baker.make(TOTPDevice, user=user)
        baker.make(BackupCode, user=user, _quantity=3)

        two_factor.disable_totp(user)
        self.assertFalse(two_factor.has_totp(user))
        self.assertEqual(BackupCode.objects.filter(user=user).count(), 0)

    def test_disable_keeps_backup_codes_if_passkey_remains(self) -> None:
        user: User = baker.make(User)
        baker.make(TOTPDevice, user=user)
        baker.make(WebAuthnCredential, user=user)
        baker.make(BackupCode, user=user, _quantity=3)

        two_factor.disable_totp(user)
        self.assertEqual(BackupCode.objects.filter(user=user).count(), 3)


class VerifyTotpCodeTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.secret = two_factor.generate_totp_secret()
        two_factor.enroll_totp(self.user, self.secret)

    def test_valid_current_code_succeeds(self) -> None:
        code = pyotp.TOTP(self.secret).now()
        self.assertTrue(two_factor.verify_totp_code(self.user, code))

    def test_wrong_code_fails(self) -> None:
        self.assertFalse(two_factor.verify_totp_code(self.user, "000000"))

    def test_blank_code_fails(self) -> None:
        self.assertFalse(two_factor.verify_totp_code(self.user, ""))

    def test_replaying_the_same_code_fails(self) -> None:
        code = pyotp.TOTP(self.secret).now()
        self.assertTrue(two_factor.verify_totp_code(self.user, code))
        self.assertFalse(two_factor.verify_totp_code(self.user, code))

    def test_code_from_adjacent_window_succeeds(self) -> None:
        totp = pyotp.TOTP(self.secret)
        earlier_code = totp.at(int(time.time()) - totp.interval)
        self.assertTrue(two_factor.verify_totp_code(self.user, earlier_code))

    def test_no_device_fails(self) -> None:
        other_user: User = baker.make(User)
        code = pyotp.TOTP(self.secret).now()
        self.assertFalse(two_factor.verify_totp_code(other_user, code))

    def test_code_with_a_middle_space_still_succeeds(self) -> None:
        code = pyotp.TOTP(self.secret).now()
        spaced = f"{code[:3]} {code[3:]}"
        self.assertTrue(two_factor.verify_totp_code(self.user, spaced))

    def test_undecryptable_secret_fails_instead_of_raising(self) -> None:
        """Regression test: a field_encryption_key rotation must not crash login.

        Before this fix, an ``InvalidToken`` raised while fetching the device
        (see ``models.fields.EncryptedTextField``) propagated straight out of
        this function uncaught - and since ``verify_login_code`` combines this
        with the backup-code fallback via ``or``, an exception here skips that
        fallback entirely (Python's ``or`` only short-circuits on a falsy
        return, not an exception), so a user with a working backup code would
        still have been locked out of login by their own broken TOTP device.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {TOTPDevice._meta.db_table} SET secret = %s WHERE user_id = %s", ["not-a-valid-fernet-token", self.user.pk])  # noqa: S608 - table name from Django _meta, not user input

        self.assertFalse(two_factor.verify_totp_code(self.user, "000000"))

    def test_undecryptable_secret_still_falls_back_to_a_backup_code(self) -> None:
        """The exact scenario the fix restores: TOTP broken, backup code still works."""
        from django.db import connection

        codes = two_factor.generate_backup_codes(self.user)

        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {TOTPDevice._meta.db_table} SET secret = %s WHERE user_id = %s", ["not-a-valid-fernet-token", self.user.pk])  # noqa: S608 - table name from Django _meta, not user input

        self.assertTrue(two_factor.verify_login_code(self.user, codes[0]))


class BackupCodeTests(TestCase):
    def test_generates_the_configured_count(self) -> None:
        user: User = baker.make(User)
        codes = two_factor.generate_backup_codes(user)
        self.assertEqual(len(codes), two_factor.BACKUP_CODE_COUNT)
        self.assertEqual(BackupCode.objects.filter(user=user).count(), two_factor.BACKUP_CODE_COUNT)

    def test_codes_are_formatted_with_a_separator(self) -> None:
        user: User = baker.make(User)
        codes = two_factor.generate_backup_codes(user)
        for code in codes:
            self.assertIn("-", code)

    def test_regenerating_invalidates_previous_codes(self) -> None:
        user: User = baker.make(User)
        first_batch = two_factor.generate_backup_codes(user)
        two_factor.generate_backup_codes(user)
        self.assertFalse(two_factor.verify_and_consume_backup_code(user, first_batch[0]))

    def test_valid_code_is_consumed_exactly_once(self) -> None:
        user: User = baker.make(User)
        codes = two_factor.generate_backup_codes(user)
        self.assertTrue(two_factor.verify_and_consume_backup_code(user, codes[0]))
        self.assertFalse(two_factor.verify_and_consume_backup_code(user, codes[0]))

    def test_verification_is_case_and_punctuation_insensitive(self) -> None:
        user: User = baker.make(User)
        codes = two_factor.generate_backup_codes(user)
        messy = codes[0].lower().replace("-", " ")
        self.assertTrue(two_factor.verify_and_consume_backup_code(user, messy))

    def test_unknown_code_fails(self) -> None:
        user: User = baker.make(User)
        two_factor.generate_backup_codes(user)
        self.assertFalse(two_factor.verify_and_consume_backup_code(user, "NOTAREALCODE"))

    def test_remaining_count_decreases_as_codes_are_used(self) -> None:
        user: User = baker.make(User)
        codes = two_factor.generate_backup_codes(user)
        self.assertEqual(two_factor.remaining_backup_code_count(user), len(codes))
        two_factor.verify_and_consume_backup_code(user, codes[0])
        self.assertEqual(two_factor.remaining_backup_code_count(user), len(codes) - 1)


class TOTPModelTests(TestCase):
    def test_totp_device_str(self) -> None:
        user: User = baker.make(User)
        device: TOTPDevice = baker.make(TOTPDevice, user=user)
        self.assertIn(str(user.pk), str(device))

    def test_backup_code_str_reflects_unused_state(self) -> None:
        code: BackupCode = baker.make(BackupCode, used_at=None)
        self.assertIn("unused", str(code))

    def test_backup_code_str_reflects_used_state(self) -> None:
        from django.utils import timezone

        code: BackupCode = baker.make(BackupCode, used_at=timezone.now())
        self.assertIn("BackupCode", str(code))
        self.assertNotIn("unused", str(code))


class TOTPSetupControllerTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_start_stashes_pending_secret_in_session(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        self.assertIn(two_factor.SESSION_PENDING_TOTP_SECRET, self.client.session)

    def test_qrcode_404s_without_a_pending_secret(self) -> None:
        response = self.client.get(reverse("settings.security.totp.qrcode"))
        self.assertEqual(response.status_code, 404)

    def test_qrcode_returns_png_with_a_pending_secret(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        response = self.client.get(reverse("settings.security.totp.qrcode"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_confirm_with_correct_code_enrolls_device(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        secret = self.client.session[two_factor.SESSION_PENDING_TOTP_SECRET]
        code = pyotp.TOTP(secret).now()

        response = self.client.post(reverse("settings.security.totp.confirm"), {"code": code})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(two_factor.has_totp(self.user))
        self.assertNotIn(two_factor.SESSION_PENDING_TOTP_SECRET, self.client.session)

    def test_confirm_with_wrong_code_does_not_enroll(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        self.client.post(reverse("settings.security.totp.confirm"), {"code": "000000"})
        self.assertFalse(two_factor.has_totp(self.user))

    def test_confirm_without_pending_secret_does_not_enroll(self) -> None:
        response = self.client.post(reverse("settings.security.totp.confirm"), {"code": "123456"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(two_factor.has_totp(self.user))

    def test_cancel_clears_pending_secret(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        self.client.post(reverse("settings.security.totp.cancel"))
        self.assertNotIn(two_factor.SESSION_PENDING_TOTP_SECRET, self.client.session)

    def test_disable_removes_device(self) -> None:
        two_factor.enroll_totp(self.user, two_factor.generate_totp_secret())
        self.client.post(reverse("settings.security.totp.disable"))
        self.assertFalse(two_factor.has_totp(self.user))

    def test_htmx_confirm_with_correct_code_returns_partial_not_a_redirect(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        secret = self.client.session[two_factor.SESSION_PENDING_TOTP_SECRET]
        code = pyotp.TOTP(secret).now()

        response = self.client.post(reverse("settings.security.totp.confirm"), {"code": code}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(two_factor.has_totp(self.user))
        self.assertContains(response, "security-settings-section-body")
        self.assertContains(response, "enabled")

    def test_htmx_confirm_with_wrong_code_shows_inline_error_not_a_redirect(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))

        response = self.client.post(reverse("settings.security.totp.confirm"), {"code": "000000"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(two_factor.has_totp(self.user))
        self.assertContains(response, "didn&#x27;t match")

    def test_htmx_confirm_strips_a_middle_space_before_verifying(self) -> None:
        self.client.post(reverse("settings.security.totp.start"))
        secret = self.client.session[two_factor.SESSION_PENDING_TOTP_SECRET]
        code = pyotp.TOTP(secret).now()
        spaced = f"{code[:3]} {code[3:]}"

        response = self.client.post(reverse("settings.security.totp.confirm"), {"code": spaced}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(two_factor.has_totp(self.user))


class BackupCodesControllerTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_blocked_without_any_second_factor(self) -> None:
        self.client.post(reverse("settings.security.backup_codes.generate"))
        self.assertEqual(BackupCode.objects.filter(user=self.user).count(), 0)

    def test_generates_codes_once_totp_is_enabled(self) -> None:
        baker.make(TOTPDevice, user=self.user)
        response = self.client.post(reverse("settings.security.backup_codes.generate"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BackupCode.objects.filter(user=self.user).count(), two_factor.BACKUP_CODE_COUNT)
        self.assertIn("new_backup_codes", self.client.session)


class LoginTwoFactorCodeViewTests(TestCase):
    """The login-time fallback that lets a TOTP-only (no passkey) account sign in."""

    def setUp(self) -> None:
        self.user: User = baker.make(User, username="totp_only", is_active=True)
        self.secret = two_factor.generate_totp_secret()
        two_factor.enroll_totp(self.user, self.secret)
        session = self.client.session
        session[account_controllers._WEBAUTHN_PENDING_USER_KEY] = self.user.pk
        session.save()

    def test_correct_totp_code_logs_in(self) -> None:
        code = pyotp.TOTP(self.secret).now()
        response = self.client.post(reverse("login.2fa.code"), {"code": code})
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_correct_backup_code_logs_in_and_consumes_it(self) -> None:
        codes = two_factor.generate_backup_codes(self.user)
        response = self.client.post(reverse("login.2fa.code"), {"code": codes[0]})
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(two_factor.remaining_backup_code_count(self.user), len(codes) - 1)

    def test_wrong_code_does_not_log_in(self) -> None:
        response = self.client.post(reverse("login.2fa.code"), {"code": "000000"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_challenge_page_offers_the_code_form_for_totp_only_account(self) -> None:
        response = self.client.get(reverse("login.2fa"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'action="/accounts/login/2fa/code/"')

    def test_login_gate_routes_totp_only_account_to_2fa(self) -> None:
        self.client.logout()
        self.user.set_password("correct horse battery staple")
        self.user.save()
        response = self.client.post(reverse("login"), {"username": "totp_only", "password": "correct horse battery staple"})
        self.assertRedirects(response, reverse("login.2fa"))
        self.assertNotIn("_auth_user_id", self.client.session)
