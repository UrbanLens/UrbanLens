"""Secondary-email verification sends are bounded - they were the one ungoverned path.

Every other arbitrary-address send goes through the `email_safety` ledger;
adding/resending verification emails had no rate limit, no resend cooldown,
and no ledger entry (PROBLEMS 2026-08-13: relay and mail-bomb, both cheap).
Both paths now consult the same per-profile caps as invites and log under
``EmailType.EMAIL_VERIFICATION``; resend also has a fixed cooldown per
address.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.email_log import EmailSendLog, EmailType


class EmailVerificationLimitTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.url = reverse("profile.edit")

    def _add(self, address: str):
        with mock.patch("urbanlens.dashboard.controllers.userprofile._send_profile_email_verification") as send:
            response = self.client.post(self.url, {"action": "add_email", "email_input": address}, HTTP_HX_REQUEST="true")
        return response, send

    def test_adding_an_email_logs_the_send_in_the_ledger(self) -> None:
        response, send = self._add("second@example.test")
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()
        self.assertTrue(EmailSendLog.objects.filter(sender=self.profile, email_type=EmailType.EMAIL_VERIFICATION).exists())

    def test_an_exhausted_ledger_blocks_the_add_send(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.userprofile._send_profile_email_verification") as send:
            with mock.patch("urbanlens.dashboard.services.security.email_safety.email_rate_limit_error", return_value="Too many emails - try later."):
                response = self.client.post(self.url, {"action": "add_email", "email_input": "relay@example.test"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        send.assert_not_called()
        self.assertFalse(self.profile.secondary_emails.exists(), "a blocked send must not leave a pending unverifiable row behind")

    def test_resend_is_cooled_down_per_address(self) -> None:
        self._add("second@example.test")
        email_id = self.profile.secondary_emails.get().pk
        with mock.patch("urbanlens.dashboard.controllers.userprofile._send_profile_email_verification") as send:
            response = self.client.post(self.url, {"action": "resend_email_verification", "email_id": email_id}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        send.assert_not_called()
        self.assertEqual(EmailSendLog.objects.filter(sender=self.profile, email_type=EmailType.EMAIL_VERIFICATION).count(), 1, "the mail-bomb path: resend within the cooldown must not send")

    def test_resend_works_after_the_cooldown(self) -> None:
        self._add("second@example.test")
        import datetime

        stale = EmailSendLog.objects.get(sender=self.profile).created - datetime.timedelta(minutes=10)
        EmailSendLog.objects.filter(sender=self.profile).update(created=stale)
        email_id = self.profile.secondary_emails.get().pk
        with mock.patch("urbanlens.dashboard.controllers.userprofile._send_profile_email_verification") as send:
            response = self.client.post(self.url, {"action": "resend_email_verification", "email_id": email_id}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()
