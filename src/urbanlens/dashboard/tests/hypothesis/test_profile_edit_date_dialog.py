"""The profile edit page's invalid-date dialog opens on load only when the posted form's dates were refused."""

from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class ProfileEditDateDialogTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.client.force_login(baker.make(User))

    def test_a_plain_visit_renders_the_dialog_closed(self) -> None:
        response = self.client.get(reverse("profile.edit"))
        self.assertContains(response, 'id="date-error-dialog"')
        self.assertNotContains(response, "data-open-on-load")

    def test_a_refused_birth_date_opens_it_with_the_reason(self) -> None:
        response = self.client.post(
            reverse("profile.edit"), {"action": "save_profile", "birth_date": date.today().isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-open-on-load")
        self.assertContains(response, "<li>You must be at least 13 years old to use this service.</li>", html=True)
