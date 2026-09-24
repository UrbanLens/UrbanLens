"""A failed login must look the same whether or not the username names an account, whatever state it is in."""

from __future__ import annotations

import re
from unittest.mock import patch

from django.contrib.auth.hashers import MD5PasswordHasher
from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import AccountKdf, TOTPDevice
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.auth.two_factor import SESSION_WEBAUTHN_PENDING_USER
from urbanlens.dashboard.services.security.e2ee import fake_auth_salt

PASSWORD = "Correct-Horse-Battery-9"
WRONG = "not-the-password"
UNKNOWN = "nobody_here"
_CSRF_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{64}(?![A-Za-z0-9])")


def _account(username: str, *, active: bool = True, usable_password: bool = True) -> User:
    user = baker.make(User, username=username, is_active=active)
    if usable_password:
        user.set_password(PASSWORD)
    else:
        user.set_unusable_password()
    user.save()
    return user


class LoginDoesNotRevealUsernameTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        SiteSettings.objects.filter(pk=1).update(login_max_attempts=3, login_ip_max_attempts=0)
        _account("active_user")
        _account("pending_user", active=False)
        _account("sso_only_user", usable_password=False)
        baker.make(TOTPDevice, user=_account("two_factor_user"))
        #: A failed attempt with each of these must be indistinguishable from one with an unknown name.
        self.failures = {
            "wrong password": ("active_user", WRONG),
            "pending account, right password": ("pending_user", PASSWORD),
            "pending account, wrong password": ("pending_user", WRONG),
            "sso-only account": ("sso_only_user", WRONG),
            "two-factor account, wrong password": ("two_factor_user", WRONG),
        }

    def _login(self, username: str, password: str = WRONG):
        return self.client.post(reverse("login"), {"username": username, "password": password})

    def _page(self, username: str, password: str = WRONG) -> tuple[int, str]:
        cache.clear()
        response = self._login(username, password)
        return response.status_code, _CSRF_TOKEN_RE.sub("CSRF", response.content.decode()).replace(username, "NAME")

    def test_every_failure_renders_the_page_an_unknown_name_gets(self) -> None:
        expected = self._page(UNKNOWN)
        for case, (username, password) in self.failures.items():
            with self.subTest(case=case):
                self.assertEqual(self._page(username, password), expected)
                self.assertNotIn(SESSION_WEBAUTHN_PENDING_USER, self.client.session)

    def test_every_failure_runs_the_password_hasher_as_often_as_an_unknown_name(self) -> None:
        def hasher_runs(username: str, password: str = WRONG) -> int:
            cache.clear()
            with patch.object(MD5PasswordHasher, "encode", autospec=True, side_effect=MD5PasswordHasher.encode) as spy:
                self._login(username, password)
            return spy.call_count

        expected = hasher_runs(UNKNOWN)
        self.assertGreater(expected, 0)
        for case, (username, password) in self.failures.items():
            with self.subTest(case=case):
                self.assertEqual(hasher_runs(username, password), expected)

    def test_an_account_and_an_unknown_name_lock_out_alike(self) -> None:
        def lockout_pages(username: str) -> list[tuple[int, str]]:
            cache.clear()
            pages = []
            for _ in range(4):
                response = self._login(username)
                pages.append(
                    (response.status_code, _CSRF_TOKEN_RE.sub("CSRF", response.content.decode()).replace(username, "N"))
                )
            return pages

        self.assertEqual(lockout_pages("active_user"), lockout_pages(UNKNOWN))


class LoginParamsDoNotRevealUsernameTests(TestCase):
    def _params(self, identifier: str) -> dict:
        return self.client.get(reverse("e2ee.login_params"), {"identifier": identifier}).json()

    def test_accounts_that_cannot_sign_in_with_a_password_get_the_decoy(self) -> None:
        _account("sso_only_user", usable_password=False)
        _account("pending_legacy", active=False)
        for username in ("sso_only_user", "pending_legacy", "Pending_Legacy"):
            with self.subTest(username=username):
                self.assertEqual(self._params(username), {"mode": "derived", "auth_salt": fake_auth_salt(username)})

    def test_an_enrolled_account_still_gets_its_real_salt(self) -> None:
        user = _account("enrolled_user")
        kdf = baker.make(AccountKdf, user=user, auth_salt="c2FsdHNhbHRzYWx0c2FsdA==")

        self.assertEqual(self._params("enrolled_user"), {"mode": "derived", "auth_salt": kdf.auth_salt})
