"""Regression test for a 2FA bypass via SSO login.

python-social-auth normally logs a user in automatically once its pipeline
finishes, with no built-in equivalent of CustomLoginView's has_second_factor()
gate - so an account with a passkey/authenticator app could previously sign
in via Google/Discord SSO and skip 2FA entirely. enforce_two_factor_for_sso
closes that gap by returning an HttpResponseRedirect, which is
python-social-auth's documented mechanism for interrupting the pipeline
(social_core.backends.base.BaseAuth.run_pipeline returns a step's result
immediately whenever it isn't a dict).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import TOTPDevice, WebAuthnCredential
from urbanlens.dashboard.services.social_auth.pipeline import enforce_two_factor_for_sso
from urbanlens.dashboard.services.two_factor import SESSION_WEBAUTHN_PENDING_REDIRECT, SESSION_WEBAUTHN_PENDING_USER


class _FakeStrategy:
    """Minimal stand-in for social_django's DjangoStrategy - only .request is used."""

    def __init__(self, request) -> None:
        self.request = request


def _request_with_session():
    request = RequestFactory().get("/accounts/complete/google-oauth2/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    return request


class EnforceTwoFactorForSsoTests(TestCase):
    def test_passes_through_for_a_new_user(self) -> None:
        user: User = baker.make(User)
        strategy = _FakeStrategy(_request_with_session())
        result = enforce_two_factor_for_sso(strategy, backend=None, user=user, is_new=True)
        self.assertIsNone(result)

    def test_passes_through_when_user_is_none(self) -> None:
        strategy = _FakeStrategy(_request_with_session())
        result = enforce_two_factor_for_sso(strategy, backend=None, user=None, is_new=False)
        self.assertIsNone(result)

    def test_passes_through_for_a_returning_user_without_a_second_factor(self) -> None:
        user: User = baker.make(User)
        strategy = _FakeStrategy(_request_with_session())
        result = enforce_two_factor_for_sso(strategy, backend=None, user=user, is_new=False)
        self.assertIsNone(result)

    def test_detours_a_returning_user_with_a_passkey(self) -> None:
        user: User = baker.make(User)
        baker.make(WebAuthnCredential, user=user)
        request = _request_with_session()
        strategy = _FakeStrategy(request)

        result = enforce_two_factor_for_sso(strategy, backend=None, user=user, is_new=False)

        self.assertIsInstance(result, HttpResponseRedirect)
        self.assertEqual(result.url, reverse("login.2fa"))
        self.assertEqual(request.session[SESSION_WEBAUTHN_PENDING_USER], user.pk)
        self.assertIn(SESSION_WEBAUTHN_PENDING_REDIRECT, request.session)

    def test_detours_a_returning_user_with_totp(self) -> None:
        user: User = baker.make(User)
        baker.make(TOTPDevice, user=user)
        request = _request_with_session()
        strategy = _FakeStrategy(request)

        result = enforce_two_factor_for_sso(strategy, backend=None, user=user, is_new=False)

        self.assertIsInstance(result, HttpResponseRedirect)
        self.assertEqual(request.session[SESSION_WEBAUTHN_PENDING_USER], user.pk)
