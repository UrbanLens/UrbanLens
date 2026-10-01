"""The Delete my account dialog's typed phrase, read by ``data-enabled-by`` in the browser, is the one the form accepts."""

from __future__ import annotations

import html as html_lib
import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import DeleteAccountForm
from urbanlens.dashboard.models.profile.model import Profile


class DeleteAccountDialogMarkupTests(TestCase):
    def test_the_gate_names_both_fields_and_the_phrase_the_form_accepts(self) -> None:
        user = baker.make(User, username="Ada_Lovelace")
        user.set_password("pw-for-this-test")
        user.save()
        Profile.objects.filter(user=user).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(user)
        page = self.client.get(reverse("settings.view")).content.decode()
        button = re.search(r'<button[^>]*id="delete-account-confirm-btn"[^>]*>', page)
        phrase = re.search(r'id="delete-account-confirm-input"[^>]*\bdata-expect="([^"]*)"', page)
        assert button is not None and phrase is not None
        self.assertIn('data-enabled-by="delete-account-password delete-account-confirm-input"', button.group(0))
        form = DeleteAccountForm(
            data={"password": "pw-for-this-test", "confirm_text": html_lib.unescape(phrase.group(1))}, user=user
        )
        self.assertTrue(form.is_valid(), form.errors)
