"""Resending a signup verification is capped per address, inside the task, so the response never changes (G2-30)."""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.email_log import EmailSendLog
from urbanlens.dashboard.services.auth.signup import resend_verification
from urbanlens.dashboard.tasks import resend_signup_verification


class ResendVerificationAddressCapTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.pending = baker.make(User, username="pending", email="pending@example.com", is_active=False)
        EmailVerification.objects.create(user=self.pending)

    def _resend_from_a_fresh_ip(self, n: int) -> None:
        """Each request from a different address, as an attacker rotating IPs would send it."""
        with tasks_run_inline(resend_signup_verification), self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("resend_verification"), {"email": "pending@example.com"}, REMOTE_ADDR=f"198.51.100.{n}"
            )

    def test_rotating_ips_cannot_mail_bomb_a_pending_address(self) -> None:
        for n in range(1, 21):
            self._resend_from_a_fresh_ip(n)

        self.assertEqual(len(mail.outbox), 1)

    def test_each_resend_is_charged_to_the_pending_account(self) -> None:
        resend_verification("pending@example.com")

        self.assertEqual(EmailSendLog.objects.filter(sender=self.pending.profile).count(), 1)

    def test_a_resend_after_the_cooldown_goes_out(self) -> None:
        resend_verification("pending@example.com")
        EmailSendLog.objects.update(created=EmailSendLog.objects.get().created - datetime.timedelta(minutes=10))

        resend_verification("pending@example.com")

        self.assertEqual(len(mail.outbox), 2)

    def test_the_accounts_email_budget_also_caps_it(self) -> None:
        with mock.patch("urbanlens.dashboard.services.auth.signup.email_rate_limit_error", return_value="spent"):
            resend_verification("pending@example.com")

        self.assertEqual(len(mail.outbox), 0)

    def test_the_signup_mail_itself_starts_the_cooldown(self) -> None:
        from urbanlens.dashboard.services.auth.signup import complete_signup

        complete_signup("newcomer", "newcomer@example.com", "!", "", None)
        resend_verification("newcomer@example.com")

        self.assertEqual([m.to for m in mail.outbox], [["newcomer@example.com"]])

    def test_a_refused_resend_keeps_the_live_link(self) -> None:
        """G2-31 is undecided: a capped resend must at least not rotate the token it did not send."""
        token = EmailVerification.objects.get(user=self.pending).token
        resend_verification("pending@example.com")
        sent_token = EmailVerification.objects.get(user=self.pending).token

        resend_verification("pending@example.com")

        self.assertNotEqual(token, sent_token)
        self.assertEqual(EmailVerification.objects.get(user=self.pending).token, sent_token)

    def test_the_page_is_identical_whether_or_not_the_cap_refused(self) -> None:
        pages = set()
        for n in (1, 2):
            with tasks_run_inline(resend_signup_verification), self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("resend_verification"), {"email": "pending@example.com"}, REMOTE_ADDR=f"198.51.100.{n}"
                )
            pages.add((response.status_code, response["Location"]))

        self.assertEqual(len(pages), 1)
