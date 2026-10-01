"""Site admin > Subscriptions' autosaving forms report their saves from the site-admin bundle (``subscriptions-page.ts``)."""

from __future__ import annotations

import json
import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

_URL = reverse("site_admin_subscriptions")
_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL)


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf'\b{name}="([^"]*)"', tag)
    return match.group(1) if match else None


def _hidden(body: str, name: str) -> str | None:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)"', body)
    return match.group(1) if match else None


class SubscriptionsSaveStatusMarkupTests(TestCase):
    def setUp(self) -> None:
        self.admin = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        Profile.objects.filter(user=self.admin).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(self.admin)
        baker.make(SubscriptionRole, slug="explorer", name="Explorer", features="")

    def _autosave_forms(self) -> list[tuple[str, str]]:
        html = self.client.get(_URL).content.decode()
        self.assertIn("dashboard/js/site-admin.js", html)
        self.assertNotIn("roleSettingsSaved", html)
        forms = [
            (tag, body)
            for tag, body in _FORM.findall(html)
            if "inline-sub-form" in (_attr(tag, "class") or "") and _attr(tag, "hx-swap") == "none"
        ]
        self.assertGreaterEqual(len(forms), 5)
        return forms

    def test_every_autosaving_form_has_a_status_beside_it(self) -> None:
        for tag, body in self._autosave_forms():
            self.assertIn('class="save-status"', body, tag)

    def test_each_forms_confirmation_names_that_form(self) -> None:
        """The client matches ``roleSettingsSaved`` to a form by its field group and role."""
        for tag, body in self._autosave_forms():
            action = _hidden(body, "action")
            response = self.client.post(
                _URL, {"action": action, "role_slug": _hidden(body, "role_slug") or ""}, HTTP_HX_REQUEST="true"
            )
            self.assertEqual(response.status_code, 204, action)
            saved = json.loads(response["HX-Trigger"])["roleSettingsSaved"]
            self.assertEqual(
                (saved["field_group"], saved["role"]), (_attr(tag, "data-field-group"), _attr(tag, "data-role")), action
            )
