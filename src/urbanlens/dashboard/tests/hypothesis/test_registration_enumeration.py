"""Signup and email-change forms must not tell anyone whether an address already has an account (P147)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.tasks import deliver_email_claim, process_signup, resend_signup_verification

PASSWORD = "Correct-Horse-Battery-9"
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"
_CSRF_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{64}(?![A-Za-z0-9])")


def _verified(username: str, email: str) -> User:
    user = baker.make(User, username=username, email=email, is_active=True)
    user.profile.verified_primary_email = user.profile.primary_email_normalized
    user.profile.save(update_fields=["verified_primary_email"])
    return user


@contextmanager
def _request_cost():
    """The queries a request ran and the mail it sent before answering; background work is not run."""
    sent_before = len(mail.outbox)
    with CaptureQueriesContext(connection) as queries:
        cost: dict = {}
        yield cost
    cost["queries"] = len(queries)
    cost["mail"] = len(mail.outbox) - sent_before


def _scrub(html: str, *values: str) -> str:
    html = _CSRF_TOKEN_RE.sub("CSRF", html)
    for value in values:
        html = html.replace(value, "VALUE")
    return html


class SignupDoesNotRevealRegistrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        SiteSettings.objects.filter(pk=1).update(signup_restricted=False)
        self.existing = _verified("existing", "taken@example.com")

    def _post(self, username: str, email: str):
        with patch(_HIBP_PATCH, return_value=False):
            return self.client.post(
                reverse("signup"),
                {"username": username, "email": email, "password1": PASSWORD, "password2": PASSWORD},
            )

    def _signup(self, username: str, email: str):
        with tasks_run_inline(process_signup), self.captureOnCommitCallbacks(execute=True):
            return self._post(username, email)

    def test_the_request_does_the_same_work_either_way(self) -> None:
        self._post("warmup", "warmup@example.com")
        self.client.logout()
        with _request_cost() as taken:
            self._post("newcomer_one", "taken@example.com")
        self.client.logout()
        with _request_cost() as fresh:
            self._post("newcomer_two", "fresh@example.com")

        self.assertEqual(taken, fresh)
        self.assertEqual(fresh["mail"], 0)

    def test_a_registered_address_gets_the_same_response_as_a_new_one(self) -> None:
        taken = self._signup("newcomer_one", "taken@example.com")
        fresh = self._signup("newcomer_two", "fresh@example.com")

        self.assertEqual(taken.status_code, fresh.status_code)
        self.assertEqual(taken["Location"], fresh["Location"])

    def test_the_check_your_inbox_page_is_the_same_either_way(self) -> None:
        self._signup("newcomer_one", "taken@example.com")
        taken = self.client.get(reverse("verify_email_sent")).content.decode()
        self.client.logout()
        self._signup("newcomer_two", "fresh@example.com")
        fresh = self.client.get(reverse("verify_email_sent")).content.decode()

        self.assertEqual(_scrub(taken, "taken@example.com"), _scrub(fresh, "fresh@example.com"))

    def test_a_gmail_variant_of_a_registered_address_is_treated_as_registered(self) -> None:
        _verified("gmailer", "jess.a.mann@gmail.com")

        self._signup("newcomer_one", "jessamann+ul@gmail.com")

        self.assertFalse(User.objects.filter(username="newcomer_one").exists())

    def test_no_second_account_is_made_and_the_address_gets_a_sign_in_notice(self) -> None:
        self._signup("newcomer_one", "taken@example.com")

        self.assertFalse(User.objects.filter(username="newcomer_one").exists())
        self.assertEqual(len(mail.outbox), 1)
        notice = mail.outbox[0]
        self.assertEqual(notice.to, ["taken@example.com"])
        self.assertIn("possibly you", notice.body)
        self.assertIn(reverse("login"), notice.body)
        self.assertIn(reverse("password_reset"), notice.body)
        self.assertNotIn("verify", notice.body.lower())

    def test_a_verified_secondary_address_counts_as_registered(self) -> None:
        ProfileEmail.objects.create(profile=self.existing.profile, email="alt@example.com", is_verified=True)

        self._signup("newcomer_one", "alt@example.com")

        self.assertFalse(User.objects.filter(username="newcomer_one").exists())
        self.assertEqual(mail.outbox[0].to, ["alt@example.com"])

    def test_a_new_address_still_gets_an_account_and_a_verification_link(self) -> None:
        self._signup("newcomer_two", "fresh@example.com")

        user = User.objects.get(username="newcomer_two")
        self.assertFalse(user.is_active)
        self.assertIn(str(user.email_verification.token), mail.outbox[0].body)

    def test_an_abandoned_unverified_signup_does_not_hold_the_address(self) -> None:
        stale = baker.make(User, username="abandoned", email="stale@example.com", is_active=False)
        verification = EmailVerification.objects.create(user=stale)
        EmailVerification.objects.filter(pk=verification.pk).update(created=timezone.now() - timedelta(days=3))

        self._signup("newcomer_three", "stale@example.com")

        self.assertTrue(User.objects.filter(username="newcomer_three").exists())
        self.assertFalse(User.objects.filter(pk=stale.pk).exists())

    def test_a_signup_still_awaiting_verification_is_not_replaced(self) -> None:
        pending = baker.make(User, username="pending", email="pending@example.com", is_active=False)
        EmailVerification.objects.create(user=pending)

        self._signup("newcomer_four", "pending@example.com")

        self.assertTrue(User.objects.filter(pk=pending.pk).exists())
        self.assertFalse(User.objects.filter(username="newcomer_four").exists())

    def test_notices_to_one_address_are_throttled(self) -> None:
        for index in range(3):
            self._signup(f"newcomer_{index}", "taken@example.com")

        self.assertEqual(len(mail.outbox), 1)


class EmailChangeDoesNotRevealRegistrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        _verified("other", "taken@example.com")
        self.user = _verified("changer", "mine@example.com")
        self.client.force_login(self.user)

    def _delivered(self, request, *args, **kwargs):
        with tasks_run_inline(deliver_email_claim), self.captureOnCommitCallbacks(execute=True):
            return request(*args, **kwargs)

    def _inline(self, value: str):
        return self._delivered(self.client.post, reverse("profile.field.update"), {"field": "email", "value": value})

    def _contact(self, value: str):
        return self._delivered(self.client.post, reverse("settings.view"), {"section": "contact", "email": value})

    def _add_secondary(self, value: str):
        return self._delivered(
            self.client.post,
            reverse("profile.edit"),
            {"action": "add_email", "email_input": value},
            HTTP_HX_REQUEST="true",
        )

    def test_each_form_does_the_same_work_either_way(self) -> None:
        forms = {
            "inline": lambda value: self.client.post(
                reverse("profile.field.update"), {"field": "email", "value": value}
            ),
            "contact": lambda value: self.client.post(reverse("settings.view"), {"section": "contact", "email": value}),
            "secondary": lambda value: self.client.post(
                reverse("profile.edit"), {"action": "add_email", "email_input": value}, HTTP_HX_REQUEST="true"
            ),
        }
        for name, submit in forms.items():
            with (
                self.subTest(name),
                patch("urbanlens.dashboard.services.auth.email_claims.email_rate_limit_error", return_value=None),
            ):
                submit("warmup@example.com")
                ProfileEmail.objects.filter(profile=self.user.profile).delete()
                with _request_cost() as taken:
                    submit("taken@example.com")
                ProfileEmail.objects.filter(profile=self.user.profile).delete()
                with _request_cost() as fresh:
                    submit("fresh@example.com")
                ProfileEmail.objects.filter(profile=self.user.profile).delete()

                self.assertEqual(taken, fresh)
                self.assertEqual(fresh["mail"], 0)

    def test_the_inline_primary_edit_answers_the_same_either_way(self) -> None:
        taken = self._inline("taken@example.com")
        fresh = self._inline("fresh@example.com")

        self.assertEqual(taken.status_code, fresh.status_code)
        self.assertEqual(
            taken.content.decode().replace("taken@example.com", "X"),
            fresh.content.decode().replace("fresh@example.com", "X"),
        )

    def test_the_settings_contact_form_answers_the_same_either_way(self) -> None:
        taken = self._contact("taken@example.com")
        fresh = self._contact("fresh@example.com")

        self.assertEqual(taken.status_code, fresh.status_code)
        self.assertEqual(taken.get("Location"), fresh.get("Location"))

    def test_adding_a_secondary_address_answers_the_same_either_way(self) -> None:
        taken = self._add_secondary("taken@example.com")
        taken_html = _scrub(taken.content.decode(), "taken@example.com")
        ProfileEmail.objects.filter(profile=self.user.profile).delete()
        fresh = self._add_secondary("fresh@example.com")

        self.assertEqual(taken.status_code, fresh.status_code)
        self.assertEqual(
            re.sub(r"email_id\" value=\"\d+", "ID", taken_html),
            re.sub(r"email_id\" value=\"\d+", "ID", _scrub(fresh.content.decode(), "fresh@example.com")),
        )

    def test_the_primary_address_does_not_change_until_the_new_one_is_verified(self) -> None:
        for change in (self._inline, self._contact):
            change("fresh@example.com")
            self.user.refresh_from_db()
            self.assertEqual(self.user.email, "mine@example.com")

    def test_following_the_link_switches_the_primary_address(self) -> None:
        self._inline("fresh@example.com")
        link = re.search(r"https?://\S+/verify/\S+?/", mail.outbox[-1].body)
        self.assertIsNotNone(link)

        self.client.get(link.group(0))

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "fresh@example.com")
        self.assertEqual(self.user.profile.verified_primary_email, "fresh@example.com")

    def test_a_taken_address_gets_a_notice_with_no_link(self) -> None:
        self._inline("taken@example.com")

        self.assertEqual(mail.outbox[-1].to, ["taken@example.com"])
        self.assertNotIn("http", mail.outbox[-1].body)
        self.assertFalse(ProfileEmail.objects.filter(profile=self.user.profile, is_verified=True).exists())

    def test_verifying_an_address_another_account_holds_is_refused(self) -> None:
        self._add_secondary("later@example.com")
        pending = ProfileEmail.objects.get(profile=self.user.profile, normalized_email="later@example.com")
        _verified("claimer", "later@example.com")

        self.client.get(reverse("profile.email.verify", args=[pending.verification_token]))

        pending.refresh_from_db()
        self.assertFalse(pending.is_verified)


class ResendDoesNotRevealRegistrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        _verified("active", "active@example.com")
        self.pending = baker.make(User, username="pending", email="pending@example.com", is_active=False)
        EmailVerification.objects.create(user=self.pending)

    def _resend(self, email: str) -> str:
        with tasks_run_inline(resend_signup_verification), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("resend_verification"), {"email": email})
        return _scrub(self.client.get(reverse("verify_email_sent")).content.decode(), email)

    def test_the_page_is_the_same_for_a_pending_an_active_and_an_unknown_address(self) -> None:
        pages = {self._resend(email) for email in ("pending@example.com", "active@example.com", "nobody@example.com")}

        self.assertEqual(len(pages), 1)

    def test_the_request_does_the_same_work_either_way(self) -> None:
        costs = []
        for email in ("pending@example.com", "nobody@example.com"):
            with _request_cost() as cost:
                self.client.post(reverse("resend_verification"), {"email": email})
            costs.append(cost)

        self.assertEqual(costs[0], costs[1])
        self.assertEqual(costs[0]["mail"], 0)

    def test_only_a_pending_account_gets_a_new_link(self) -> None:
        self._resend("pending@example.com")
        self._resend("active@example.com")
        self._resend("nobody@example.com")

        self.assertEqual([message.to for message in mail.outbox], [["pending@example.com"]])
        self.assertIn(str(EmailVerification.objects.get(user=self.pending).token), mail.outbox[0].body)
