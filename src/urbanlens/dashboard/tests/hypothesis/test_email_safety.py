"""Tests for the outbound-email safety controls.

Covers:
- hash_email - one-way, normalization-aware hashing (property-based)
- get_email_limits - site default vs. subscription-role override resolution
  (largest wins, 0 = unlimited)
- email_rate_limit_error - hour/day/month windows
- has_sent_join_email / record_email_sent - one join email per address ever
- invite_by_email - returns 429 at the cap and never re-emails an address
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.email_log import EmailSendLog, EmailType
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.email_safety import (
    email_rate_limit_error,
    get_email_limits,
    has_sent_join_email,
    hash_email,
    record_email_sent,
)

_EMAILS = st.emails()


class HashEmailTests(SimpleTestCase):
    """hash_email is deterministic, normalized, and never stores the address."""

    @given(email=_EMAILS)
    def test_hash_is_64_hex_chars_and_deterministic(self, email):
        digest = hash_email(email)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, hash_email(email))
        int(digest, 16)  # raises if not hex

    def test_gmail_variants_hash_identically(self):
        self.assertEqual(hash_email("Jake.Smith+spam@gmail.com"), hash_email("jakesmith@gmail.com"))

    def test_case_is_ignored(self):
        self.assertEqual(hash_email("Someone@Example.COM"), hash_email("someone@example.com"))

    @given(email=_EMAILS)
    def test_hash_does_not_contain_address(self, email):
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4:
            self.assertNotIn(local, hash_email(email))


class EmailLimitResolutionTests(TestCase):
    """Site defaults + role overrides resolve like storage quotas (max wins, 0=unlimited)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User, username="limits-user")
        self.profile = self.user.profile
        settings = SiteSettings.get_current()
        settings.email_limit_per_hour = 2
        settings.email_limit_per_day = 5
        settings.email_limit_per_month = 10
        settings.save()

    def test_defaults_apply_without_roles(self):
        self.assertEqual(get_email_limits(self.profile), (2, 5, 10))

    def test_role_raises_limit(self):
        role = SubscriptionRole.objects.create(slug="mailer", name="Mailer", email_limit_per_hour=50)
        grant_subscription(self.user, role, self.user, None)
        per_hour, per_day, per_month = get_email_limits(self.profile)
        self.assertEqual(per_hour, 50)
        self.assertEqual((per_day, per_month), (5, 10))

    def test_zero_means_unlimited(self):
        role = SubscriptionRole.objects.create(slug="unlimited", name="Unlimited", email_limit_per_day=0)
        grant_subscription(self.user, role, self.user, None)
        _hour, per_day, _month = get_email_limits(self.profile)
        self.assertIsNone(per_day)

    def test_rate_limit_error_after_hourly_cap(self):
        self.assertIsNone(email_rate_limit_error(self.profile))
        record_email_sent(self.profile, "a@example.com", EmailType.JOIN_INVITE)
        record_email_sent(self.profile, "b@example.com", EmailType.JOIN_INVITE)
        error = email_rate_limit_error(self.profile)
        self.assertIsNotNone(error)
        self.assertIn("hour", error)

    def test_old_sends_do_not_count_against_hour(self):
        for address in ("a@example.com", "b@example.com"):
            log = record_email_sent(self.profile, address, EmailType.JOIN_INVITE)
            EmailSendLog.objects.filter(pk=log.pk).update(created=timezone.now() - datetime.timedelta(hours=2))
        self.assertIsNone(email_rate_limit_error(self.profile))

    def test_monthly_cap_counts_older_sends(self):
        for i in range(10):
            log = record_email_sent(self.profile, f"user{i}@example.com", EmailType.JOIN_INVITE)
            EmailSendLog.objects.filter(pk=log.pk).update(created=timezone.now() - datetime.timedelta(days=i + 1))
        error = email_rate_limit_error(self.profile)
        self.assertIsNotNone(error)
        self.assertIn("month", error)


class JoinEmailDedupTests(TestCase):
    """A user sends at most one join-the-site email to a given address, ever."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User, username="dedup-user")
        self.profile = self.user.profile

    def test_no_history_allows_send(self):
        self.assertFalse(has_sent_join_email(self.profile, "new@example.com"))

    def test_join_invite_blocks_resend(self):
        record_email_sent(self.profile, "new@example.com", EmailType.JOIN_INVITE)
        self.assertTrue(has_sent_join_email(self.profile, "new@example.com"))

    def test_visit_invite_also_blocks_join_invite(self):
        record_email_sent(self.profile, "new@example.com", EmailType.VISIT_INVITE)
        self.assertTrue(has_sent_join_email(self.profile, "new@example.com"))

    def test_gmail_variant_blocked_too(self):
        record_email_sent(self.profile, "jakesmith@gmail.com", EmailType.JOIN_INVITE)
        self.assertTrue(has_sent_join_email(self.profile, "Jake.Smith+other@gmail.com"))

    def test_other_sender_not_blocked(self):
        other = baker.make(User, username="other-sender")
        record_email_sent(other.profile, "new@example.com", EmailType.JOIN_INVITE)
        self.assertFalse(has_sent_join_email(self.profile, "new@example.com"))


class InviteByEmailSafetyTests(TestCase):
    """The invite-a-friend endpoint enforces the caps and the dedup rule."""

    def setUp(self) -> None:
        super().setUp()
        self.inviter = baker.make(User, username="capped-inviter", email="capped@example.com")
        self.client.force_login(self.inviter)
        self.url = reverse("friend.invite_email")

    def test_429_when_over_limit(self):
        settings = SiteSettings.get_current()
        settings.email_limit_per_hour = 1
        settings.save()
        record_email_sent(self.inviter.profile, "someone@example.com", EmailType.JOIN_INVITE)

        response = self.client.post(self.url, {"email": "another@example.com"})

        self.assertEqual(response.status_code, 429)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_second_invite_to_same_address_sends_no_email(self, mock_send):
        self.client.post(self.url, {"email": "brandnew@example.com"})
        self.assertEqual(mock_send.call_count, 1)

        self.client.post(self.url, {"email": "brandnew@example.com"})

        self.assertEqual(mock_send.call_count, 1)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_send_is_logged_with_hash_only(self, mock_send):
        self.client.post(self.url, {"email": "brandnew@example.com"})

        log = EmailSendLog.objects.get(sender=self.inviter.profile)
        self.assertEqual(log.email_type, EmailType.JOIN_INVITE)
        self.assertEqual(log.recipient_hash, hash_email("brandnew@example.com"))
        self.assertNotIn("brandnew", log.recipient_hash)
