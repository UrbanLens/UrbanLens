"""A failed verification email printed the verification link on the page.

`SignupView._send_verification_email` catches an SMTP failure and stores the
verify URL in the session; `VerifyEmailSentView` renders it. The template labels
that block "Development mode - verification link:", but **nothing checks that it
is development mode** - not `settings.DEBUG`, not a setting of any kind. The only
condition is that sending failed.

Following the link sets `user.is_active = True` (`VerifyEmailView`), so the link
is the whole of the verification. Printing it to whoever submitted the form means
email verification stops proving control of the address for exactly as long as
the mail server is unhealthy: sign up as somebody else, with a password of your
choosing, and if the send fails you are handed the link and activate the account
yourself.

That is not a hypothetical condition for this application - the mail server going
slow or unreachable is the failure the `EMAIL_TIMEOUT` work exists to bound, and
an unthrottled caller could previously spend a provider's send quota to induce
it.

`ResendVerificationView` shares the helper and so has the same shape, though its
reach is narrower: it only acts on an existing account that is still inactive.

The fix is to gate the display on `settings.DEBUG`, which is what the template
already claims. These tests assert the attacker's view - that the link is absent
from the response - rather than that a flag is set, because the flag is not what
does the damage.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import EmailVerification

SEND = "django.core.mail.EmailMultiAlternatives.send"
#: Signup validates the password against Have I Been Pwned, which the suite's
#: network guard refuses - so every signup test stubs it.
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"
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
        self.assertNotContains(
            response, str(verification.token), msg_prefix="the verification token was printed on the page"
        )

    @override_settings(DEBUG=False)
    def test_the_account_is_still_inactive_afterwards(self) -> None:
        """The link is the whole of the verification, so possessing it is the harm."""
        self._signup_while_mail_is_broken()

        self.assertFalse(User.objects.get(email=VICTIM).is_active)


class ButDevelopmentStillGetsItTests(_SignupCase):
    """The affordance is real and worth keeping where the template says it applies."""

    @override_settings(DEBUG=True)
    def test_debug_still_prints_the_link(self) -> None:
        response = self._signup_while_mail_is_broken()

        verification = EmailVerification.objects.filter(user__email=VICTIM).first()
        self.assertContains(response, str(verification.token))


class ASuccessfulSendNeverShowsItEitherTests(_SignupCase):
    """The anti-vacuity half: a page that never shows a link would pass the tests above."""

    @override_settings(DEBUG=True)
    def test_nothing_is_printed_when_the_mail_actually_went(self) -> None:
        with mock.patch(_HIBP_PATCH, return_value=False):
            self.client.post(
                reverse("signup"),
                data={
                    "username": "ordinary",
                    "email": "ordinary@example.com",
                    "password1": "Correct-Horse-9271!",
                    "password2": "Correct-Horse-9271!",
                },
            )

        response = self.client.get(reverse("verify_email_sent"))

        verification = EmailVerification.objects.filter(user__email="ordinary@example.com").first()
        self.assertIsNotNone(verification)
        self.assertNotContains(response, str(verification.token))


class TheResendDoorBehavesTheSameTests(TestCase):
    """It shares the helper, so it shares the defect and must share the fix."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.victim = baker.make(User, email=VICTIM, is_active=False)

    @override_settings(DEBUG=False)
    def test_asking_to_resend_someone_elses_verification_prints_nothing(self) -> None:
        with mock.patch(SEND, side_effect=OSError("mail server unreachable")):
            self.client.post(reverse("resend_verification"), data={"email": VICTIM})
        response = self.client.get(reverse("verify_email_sent"))

        verification = EmailVerification.objects.filter(user=self.victim).first()
        self.assertIsNotNone(verification, "no verification was created, so this test is measuring nothing")
        self.assertNotContains(response, str(verification.token))
