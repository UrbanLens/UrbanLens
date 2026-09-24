"""The password policy judges the password a person typed, never the credential their browser derived from it.

A browser enrolled in derived auth checks the typed password against ``AUTH_PASSWORD_VALIDATORS`` through
``validate_password_policy`` and submits only the derived credential, with its salt. Validating that credential again
refused random signups as "too similar to the email address" whenever its base64 happened to share enough characters.
"""

from __future__ import annotations

import base64
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import AccountKdf
from urbanlens.dashboard.tasks import process_signup

_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"
EMAIL = "qwertyuiopasdf@example.com"
#: Lowercase letters only and nearly the address itself: every policy refuses it as a typed password.
LOOKALIKE = "qwertyuiopasdfexamplecom"
SALT = base64.b64encode(b"sixteen byte salt").decode()


class SignupDerivedCredentialTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(_HIBP_PATCH, return_value=False)
        self.hibp = patcher.start()
        self.addCleanup(patcher.stop)

    def _signup(self, **extra: str):
        data = {"username": "lookalike_owner", "email": EMAIL, "password1": LOOKALIKE, "password2": LOOKALIKE, **extra}
        with tasks_run_inline(process_signup), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("signup"), data)

    def test_a_derived_credential_is_not_judged_as_a_password(self) -> None:
        response = self._signup(e2ee_auth_salt=SALT)

        self.assertRedirects(response, reverse("verify_email_sent"), fetch_redirect_response=False)
        user = User.objects.get(username="lookalike_owner")
        self.assertTrue(AccountKdf.objects.for_user(user).exists())
        self.hibp.assert_not_called()

    def test_a_typed_password_is_still_judged(self) -> None:
        response = self._signup()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too similar")
        self.assertFalse(User.objects.filter(username="lookalike_owner").exists())

    def test_a_malformed_salt_does_not_skip_the_policy(self) -> None:
        response = self._signup(e2ee_auth_salt="not base64!")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="lookalike_owner").exists())


class ResetDerivedCredentialTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(_HIBP_PATCH, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = baker.make(User, email=EMAIL, is_active=True)
        self.user.set_password("The Old Password 17!")  # nosec B106 - test fixture password
        self.user.save(update_fields=["password"])

    def _reset(self, **extra: str):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.client.get(
            reverse("password_reset_confirm", args=[uidb64, default_token_generator.make_token(self.user)]), follow=True
        )
        url = reverse("password_reset_confirm", args=[uidb64, "set-password"])
        return self.client.post(url, {"new_password1": LOOKALIKE, "new_password2": LOOKALIKE, **extra})

    def test_a_derived_credential_is_not_judged_as_a_password(self) -> None:
        response = self._reset(e2ee_auth_salt=SALT)

        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(LOOKALIKE))

    def test_a_typed_password_is_still_judged(self) -> None:
        response = self._reset()

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(LOOKALIKE))
