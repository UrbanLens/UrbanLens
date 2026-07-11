"""Tests for logging in with an email address instead of a username."""

from __future__ import annotations

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.services.auth_backend import EmailOrUsernameModelBackend


class EmailOrUsernameModelBackendTests(TestCase):
    """The backend accepts a username, a primary email, or a verified secondary email."""

    def setUp(self) -> None:
        self.user = baker.make(User, username="explorer1", is_active=True)
        self.user.set_password("correct horse battery staple")
        self.user.email = "explorer@example.com"
        self.user.save()

    def test_authenticates_with_username(self) -> None:
        result = authenticate(username="explorer1", password="correct horse battery staple")
        self.assertEqual(result, self.user)

    def test_authenticates_with_primary_email(self) -> None:
        result = authenticate(username="explorer@example.com", password="correct horse battery staple")
        self.assertEqual(result, self.user)

    def test_authenticates_with_gmail_dot_variant_of_primary_email(self) -> None:
        self.user.email = "jakesmith@gmail.com"
        self.user.save(update_fields=["email"])
        result = authenticate(username="Jake.Smith+login@gmail.com", password="correct horse battery staple")
        self.assertEqual(result, self.user)

    def test_authenticates_with_verified_secondary_email(self) -> None:
        ProfileEmail.objects.create(profile=self.user.profile, email="alt@example.com", is_verified=True)
        result = authenticate(username="alt@example.com", password="correct horse battery staple")
        self.assertEqual(result, self.user)

    def test_does_not_authenticate_with_unverified_secondary_email(self) -> None:
        ProfileEmail.objects.create(profile=self.user.profile, email="alt@example.com", is_verified=False)
        result = authenticate(username="alt@example.com", password="correct horse battery staple")
        self.assertIsNone(result)

    def test_wrong_password_fails(self) -> None:
        result = authenticate(username="explorer@example.com", password="wrong password")
        self.assertIsNone(result)

    def test_unknown_email_fails(self) -> None:
        result = authenticate(username="nobody@example.com", password="correct horse battery staple")
        self.assertIsNone(result)

    def test_inactive_account_does_not_authenticate_via_email(self) -> None:
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        result = authenticate(username="explorer@example.com", password="correct horse battery staple")
        self.assertIsNone(result)

    def test_backend_direct_call_matches_module_level_authenticate(self) -> None:
        backend = EmailOrUsernameModelBackend()
        result = backend.authenticate(request=None, username="explorer@example.com", password="correct horse battery staple")
        self.assertEqual(result, self.user)


class LoginViewEmailTests(TestCase):
    """The login page itself accepts an email address in the username field."""

    def test_login_view_accepts_email(self) -> None:
        user = baker.make(User, username="explorer2", is_active=True, email="explorer2@example.com")
        user.set_password("correct horse battery staple")
        user.save()

        response = self.client.post(
            reverse("login"),
            {"username": "explorer2@example.com", "password": "correct horse battery staple"},
        )

        self.assertEqual(response.status_code, 302)
