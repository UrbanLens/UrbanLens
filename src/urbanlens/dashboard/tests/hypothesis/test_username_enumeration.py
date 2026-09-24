"""No surface may say a username is taken in words that differ from how it refuses a malformed one."""

from __future__ import annotations

import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.auth.username import USERNAME_RULES, USERNAME_UNAVAILABLE

PASSWORD = "Correct-Horse-Battery-9"
_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"
_CSRF_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{64}(?![A-Za-z0-9])")


def _scrub(html: str, value: str) -> str:
    return _CSRF_TOKEN_RE.sub("CSRF", html).replace(value, "VALUE")


#: Each is refused; none may be told apart from the others.
REFUSED_USERNAMES = {
    "taken": "existing_user",
    "taken_other_case": "Existing_User",
    "too_close": "ex1sting_user",
    "reserved": "demo-abcd1234-0",
    "too_short": "ab",
    "too_long": "a" * 31,
    "bad_characters": "bad name!",
    "django_allowed_characters": "has.dot@sign",
}


class SignupUsernameRefusalTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        SiteSettings.objects.filter(pk=1).update(signup_restricted=False)
        baker.make(User, username="existing_user", is_active=True)

    def _post(self, username: str):
        with patch(_HIBP_PATCH, return_value=False):
            return self.client.post(
                reverse("signup"),
                {"username": username, "email": "fresh@example.com", "password1": PASSWORD, "password2": PASSWORD},
            )

    def test_every_refusal_gives_the_one_generic_error(self) -> None:
        for case, username in REFUSED_USERNAMES.items():
            with self.subTest(case=case):
                response = self._post(username)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"].errors.get("username"), [USERNAME_UNAVAILABLE])

    def test_the_rendered_page_is_the_same_for_a_taken_and_a_malformed_name(self) -> None:
        taken = _scrub(self._post("existing_user").content.decode(), "existing_user")
        malformed = _scrub(self._post("bad!name").content.decode(), "bad!name")

        self.assertEqual(taken, malformed)

    def test_the_format_rules_are_shown_before_anything_is_typed(self) -> None:
        response = self.client.get(reverse("signup"))

        self.assertContains(response, USERNAME_RULES)


class RenameUsernameRefusalTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User, username="existing_user", is_active=True)
        self.me = baker.make(User, username="renamer", is_active=True)
        self.client.force_login(self.me)

    def test_the_availability_check_gives_one_reason_for_every_refusal(self) -> None:
        for case, username in REFUSED_USERNAMES.items():
            with self.subTest(case=case):
                response = self.client.get(reverse("profile.field.update"), {"field": "username", "value": username})
                self.assertEqual(response.json(), {"available": False, "reason": USERNAME_UNAVAILABLE})

    def test_saving_answers_every_refusal_with_the_same_status_and_error(self) -> None:
        for case, username in REFUSED_USERNAMES.items():
            with self.subTest(case=case):
                response = self.client.post(reverse("profile.field.update"), {"field": "username", "value": username})
                self.assertEqual((response.status_code, response.json()), (400, {"error": USERNAME_UNAVAILABLE}))
        self.me.refresh_from_db()
        self.assertEqual(self.me.username, "renamer")

    def test_keeping_your_own_name_in_another_case_is_allowed(self) -> None:
        response = self.client.post(reverse("profile.field.update"), {"field": "username", "value": "Renamer"})

        self.assertEqual(response.json(), {"ok": True})
