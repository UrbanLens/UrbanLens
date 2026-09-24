"""A failed verification email must not hand the link to whoever submitted the form.

Following the link activates the account, so printing it on the page would let anyone sign up as somebody else
whenever the mail server is down. Signup and resend now send from a background task, so the link never reaches
the response; in development it is logged instead.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import EmailVerification
from urbanlens.dashboard.tasks import process_signup, resend_signup_verification

SEND = "django.core.mail.EmailMultiAlternatives.send"
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"
_LOGGER = "urbanlens.dashboard.services.auth.signup"
VICTIM = "victim@example.com"


class _SignupCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin

    def _signup_while_mail_is_broken(self, email: str = VICTIM):
        """Sign up as *email* with the mail server refusing, then load the next page."""
        with (
            mock.patch(_HIBP_PATCH, return_value=False),
            mock.patch(SEND, side_effect=OSError("mail server unreachable")),
            tasks_run_inline(process_signup),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(
                reverse("signup"),
                data={
                    "username": "attacker",
                    "email": email,
                    "password1": "Correct-Horse-9271!",
                    "password2": "Correct-Horse-9271!",
                },
            )
        return self.client.get(reverse("verify_email_sent"))


class TheLinkIsNotHandedToTheSubmitterTests(_SignupCase):
    @override_settings(DEBUG=False)
    def test_a_failed_send_does_not_print_the_verification_link(self) -> None:
        response = self._signup_while_mail_is_broken()

        verification = EmailVerification.objects.filter(user__email=VICTIM).first()
        self.assertIsNotNone(verification, "no account was created, so this test is measuring nothing")
        self.assertNotContains(response, str(verification.token))

    @override_settings(DEBUG=True)
    def test_not_even_in_development(self) -> None:
        response = self._signup_while_mail_is_broken()

        verification = EmailVerification.objects.filter(user__email=VICTIM).first()
        self.assertIsNotNone(verification)
        self.assertNotContains(response, str(verification.token))

    @override_settings(DEBUG=False)
    def test_the_account_is_still_inactive_afterwards(self) -> None:
        self._signup_while_mail_is_broken()

        self.assertFalse(User.objects.get(email=VICTIM).is_active)


class DevelopmentLogsTheLinkTests(_SignupCase):
    @override_settings(DEBUG=True)
    def test_debug_logs_the_link(self) -> None:
        with self.assertLogs(_LOGGER, "WARNING") as logs:
            self._signup_while_mail_is_broken()

        verification = EmailVerification.objects.get(user__email=VICTIM)
        self.assertIn(str(verification.token), "\n".join(logs.output))

    @override_settings(DEBUG=False)
    def test_production_does_not_log_it(self) -> None:
        with self.assertLogs(_LOGGER, "DEBUG") as logs:
            self._signup_while_mail_is_broken()

        verification = EmailVerification.objects.get(user__email=VICTIM)
        self.assertNotIn(str(verification.token), "\n".join(logs.output))


class TheResendDoorBehavesTheSameTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.victim = baker.make(User, email=VICTIM, is_active=False)

    @override_settings(DEBUG=True)
    def test_asking_to_resend_someone_elses_verification_prints_nothing(self) -> None:
        with (
            mock.patch(SEND, side_effect=OSError("mail server unreachable")),
            tasks_run_inline(resend_signup_verification),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(reverse("resend_verification"), data={"email": VICTIM})
        response = self.client.get(reverse("verify_email_sent"))

        verification = EmailVerification.objects.filter(user=self.victim).first()
        self.assertIsNotNone(verification, "no verification was created, so this test is measuring nothing")
        self.assertNotContains(response, str(verification.token))
