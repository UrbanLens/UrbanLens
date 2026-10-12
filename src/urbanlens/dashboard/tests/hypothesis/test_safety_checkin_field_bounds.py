"""A check-in's title and contact emails are refused when they would not fit their columns, rather than answered with a 500."""

from __future__ import annotations

import datetime as dt

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import CHECKIN_TITLE_MAX_LENGTH, SafetyCheckin
from urbanlens.dashboard.services.visits.safety import validate_notifiable_contacts

#: Found by the write-route sweep: a 400-character value overflowed ``email_normalized`` (254 characters).
_TOO_LONG = "9" * 400


class ContactEmailShapeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.owner = self.user.profile
        self.checkin = baker.make(SafetyCheckin, profile=self.owner, title="Tunnel walk")

    def test_a_value_that_is_not_a_storable_address_is_rejected(self) -> None:
        for email in (_TOO_LONG, f"{'a' * 250}@example.com", "not-an-address"):
            with self.subTest(email=email[:20]):
                allowed, rejected = validate_notifiable_contacts(self.owner, [(None, email, "")])

                self.assertEqual(allowed, [])
                self.assertEqual(len(rejected), 1)

    def test_an_address_is_still_accepted(self) -> None:
        allowed, rejected = validate_notifiable_contacts(self.owner, [(None, "friend@example.com", "")])

        self.assertEqual(allowed, [(None, "friend@example.com", "")])
        self.assertEqual(rejected, [])

    def test_the_edit_form_answers_an_overlong_contact_without_a_server_error(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("safety.checkin.detail", kwargs={"checkin_slug": self.checkin.slug}),
            {"contact_emails": [_TOO_LONG]},
        )

        self.assertLess(response.status_code, 500)
        self.assertFalse(self.checkin.contacts.exists())

    def test_an_overlong_title_is_refused_by_an_edit(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("safety.checkin.detail", kwargs={"checkin_slug": self.checkin.slug}),
            {"title": "t" * (CHECKIN_TITLE_MAX_LENGTH + 1)},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f"Titles can be at most {CHECKIN_TITLE_MAX_LENGTH} characters", " ".join(response.json()["warnings"])
        )
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "Tunnel walk")

    def test_a_title_at_the_limit_is_kept(self) -> None:
        self.client.force_login(self.user)

        self.client.post(
            reverse("safety.checkin.detail", kwargs={"checkin_slug": self.checkin.slug}),
            {"title": "t" * CHECKIN_TITLE_MAX_LENGTH},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "t" * CHECKIN_TITLE_MAX_LENGTH)

    def test_an_overlong_title_is_refused_by_the_create_form(self) -> None:
        self.checkin.delete()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("safety.checkin.create"),
            {
                "title": "t" * (CHECKIN_TITLE_MAX_LENGTH + 1),
                "checkin_by": (timezone.now() + dt.timedelta(hours=2)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SafetyCheckin.objects.filter(profile=self.owner).exists())
