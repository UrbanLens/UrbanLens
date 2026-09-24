"""The outbound-mail guard hands the relay no address a mailbox cannot exist at."""

from __future__ import annotations

import smtplib

from django.core import mail
from django.core.mail import EmailMessage, get_connection
from django.test import override_settings

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.security.mail_guard import RecipientGuardEmailBackend, is_undeliverable_address

_GUARD = "urbanlens.dashboard.services.security.mail_guard.RecipientGuardEmailBackend"
_LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


class UndeliverableAddressTests(SimpleTestCase):
    def test_reserved_domains(self) -> None:
        for address in (
            "a@e2e.invalid",
            "a@site.test",
            "a@example",
            "a@localhost",
            "a@example.com",
            "Name <a@EXAMPLE.ORG>",
            "a@example.net.",
            "a@mail.example.com",
        ):
            with self.subTest(address=address):
                self.assertTrue(is_undeliverable_address(address))

    def test_gmail_names_gmail_never_issues(self) -> None:
        for address in (
            "ul-e2e-1a2b3c4d@gmail.com",
            "ul-e2e-1a2b3c4d+urbanlens@gmail.com",
            "u.l-e.2.e-x+y@googlemail.com",
            "under_score@gmail.com",
            "+tag@gmail.com",
        ):
            with self.subTest(address=address):
                self.assertTrue(is_undeliverable_address(address))

    def test_real_looking_addresses_are_delivered(self) -> None:
        for address in (
            "plain@gmail.com",
            "user+urbanlens@gmail.com",
            "s.a.m.a.rivera+ul@googlemail.com",
            "first.last-name@mailbox.org",
            "a_b@outlook.com",
            "a@notexample.com",
        ):
            with self.subTest(address=address):
                self.assertFalse(is_undeliverable_address(address))

    @settings(max_examples=200, deadline=None)
    @given(
        mailbox=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20),
        marker=st.sampled_from("-_'!#$%&*=?^`{|}~"),
        position=st.integers(min_value=0, max_value=20),
        tag=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", max_size=8),
    )
    def test_any_gmail_mailbox_holding_a_character_gmail_never_issues_is_refused(
        self, mailbox: str, marker: str, position: int, tag: str
    ) -> None:
        local = mailbox[:position] + marker + mailbox[position:]
        self.assertTrue(is_undeliverable_address(f"{local}+{tag}@gmail.com"))

    @settings(max_examples=200, deadline=None)
    @given(
        mailbox=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.", min_size=1, max_size=20).filter(
            lambda s: s.strip(".")
        ),
        tag=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", max_size=8),
    )
    def test_any_gmail_name_gmail_could_issue_is_delivered(self, mailbox: str, tag: str) -> None:
        self.assertFalse(is_undeliverable_address(f"{mailbox}+{tag}@gmail.com"))


@override_settings(EMAIL_DELIVERY_BACKEND=_LOCMEM)
class RecipientGuardEmailBackendTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        mail.outbox = []

    def _send(
        self, *, to: list[str], cc: list[str] | None = None, bcc: list[str] | None = None, fail_silently: bool = False
    ) -> int:
        connection = get_connection(_GUARD, fail_silently=fail_silently)
        return EmailMessage(
            "Subject", "Body", "noreply@urbanlens.org", to=to, cc=cc, bcc=bcc, connection=connection
        ).send(fail_silently=fail_silently)

    def test_a_message_to_only_undeliverable_addresses_never_reaches_the_delivery_backend(self) -> None:
        with self.assertRaises(smtplib.SMTPRecipientsRefused) as raised:
            self._send(to=["ul-e2e-1a2b3c4d+urbanlens@gmail.com"], cc=["someone@e2e.invalid"])
        self.assertEqual(
            set(raised.exception.recipients), {"ul-e2e-1a2b3c4d+urbanlens@gmail.com", "someone@e2e.invalid"}
        )
        self.assertEqual(mail.outbox, [])

    def test_fail_silently_refuses_without_raising(self) -> None:
        self.assertEqual(self._send(to=["ul-e2e-1a2b3c4d@gmail.com"], fail_silently=True), 0)
        self.assertEqual(mail.outbox, [])

    def test_undeliverable_recipients_are_dropped_and_the_rest_delivered(self) -> None:
        self.assertEqual(
            self._send(to=["ul-e2e-x@gmail.com", "plain@gmail.com"], bcc=["b@site.test", "bcc@mailbox.org"]), 1
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(), ["plain@gmail.com", "bcc@mailbox.org"])

    def test_deliverable_mail_is_untouched(self) -> None:
        self.assertEqual(self._send(to=["user+urbanlens@gmail.com"]), 1)
        self.assertEqual(mail.outbox[0].to, ["user+urbanlens@gmail.com"])

    def test_a_batch_sends_only_the_deliverable_messages(self) -> None:
        connection = get_connection(_GUARD)
        messages = [
            EmailMessage("s", "b", "noreply@urbanlens.org", to=[to]) for to in ("ul-e2e-x@gmail.com", "plain@gmail.com")
        ]
        self.assertEqual(connection.send_messages(messages), 1)
        self.assertEqual([message.to for message in mail.outbox], [["plain@gmail.com"]])

    @override_settings(EMAIL_DELIVERY_BACKEND=_GUARD)
    def test_refuses_to_wrap_itself(self) -> None:
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            RecipientGuardEmailBackend()


class GuardIsTheConfiguredBackendTests(SimpleTestCase):
    """Test runs swap EMAIL_BACKEND for locmem, so the wiring is read from the settings module itself."""

    def test_every_deployment_sends_through_the_guard(self) -> None:
        from urbanlens.UrbanLens.settings import base

        self.assertEqual(base.EMAIL_BACKEND, _GUARD)
        self.assertNotEqual(base.EMAIL_DELIVERY_BACKEND, _GUARD)
