"""Tests for invite-only signup restriction logic in SignupView."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.account import SignupView
from urbanlens.dashboard.models.site_settings import SiteSettings


def _get_response(factory: RequestFactory, method: str = "get", **query_params):
    """Build a GET or POST request to the signup view and dispatch it."""
    if method == "get":
        url = "/accounts/signup/"
        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            url = f"{url}?{qs}"
        request = factory.get(url)
    else:
        request = factory.post("/accounts/signup/", data=query_params)

    from django.contrib.auth.models import AnonymousUser
    request.user = AnonymousUser()
    return SignupView.as_view()(request)


class SignupOpenTests(TestCase):
    """When signup_restricted=False all visitors can reach the signup form."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        SiteSettings.objects.filter(pk=1).update(signup_restricted=False)

    def test_unauthenticated_user_can_reach_signup(self) -> None:
        response = _get_response(self.factory)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_post_without_invite_is_not_blocked(self) -> None:
        response = _get_response(self.factory, method="post")
        # POST without valid data returns the form again (200/form errors), not a 403.
        self.assertNotEqual(response.status_code, 403)


class SignupRestrictedTests(TestCase):
    """When signup_restricted=True only requests with an invite token may proceed."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        SiteSettings.objects.filter(pk=1).update(signup_restricted=True)

    def test_get_without_invite_returns_403(self) -> None:
        response = _get_response(self.factory)
        self.assertEqual(response.status_code, 403)

    def test_get_with_invite_token_is_allowed(self) -> None:
        response = _get_response(self.factory, invite="abc123")
        # Any status other than 403 means the view didn't block.
        self.assertNotEqual(response.status_code, 403)

    def test_post_without_invite_returns_403(self) -> None:
        response = _get_response(self.factory, method="post")
        self.assertEqual(response.status_code, 403)

    def test_post_with_invite_token_is_allowed(self) -> None:
        response = _get_response(self.factory, method="post", invite="abc123")
        self.assertNotEqual(response.status_code, 403)

    def test_403_response_contains_restricted_content(self) -> None:
        response = _get_response(self.factory)
        self.assertEqual(response.status_code, 403)
        if hasattr(response, "content"):
            content = response.content.decode()
            self.assertIn("invite", content.lower())

    def test_empty_invite_token_is_rejected(self) -> None:
        response = _get_response(self.factory, invite="")
        self.assertEqual(response.status_code, 403)


class SignupAuthenticatedRedirectTests(TestCase):
    """Authenticated users are always redirected regardless of restriction state."""

    def setUp(self) -> None:
        self.factory = RequestFactory()

    def _get_as_user(self, user: User) -> object:
        request = self.factory.get("/accounts/signup/")
        request.user = user
        return SignupView.as_view()(request)

    def test_authenticated_user_redirected_when_restricted(self) -> None:
        SiteSettings.objects.filter(pk=1).update(signup_restricted=True)
        user = baker.make(User, is_active=True)
        response = self._get_as_user(user)
        self.assertEqual(response.status_code, 302)

    def test_authenticated_user_redirected_when_open(self) -> None:
        SiteSettings.objects.filter(pk=1).update(signup_restricted=False)
        user = baker.make(User, is_active=True)
        response = self._get_as_user(user)
        self.assertEqual(response.status_code, 302)
