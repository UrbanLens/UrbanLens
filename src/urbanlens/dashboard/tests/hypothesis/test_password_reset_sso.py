"""Tests for UL-257: password reset must not silently drop SSO-only accounts.

Django's stock PasswordResetForm.get_users() filters out any account with
has_usable_password() == False, while PasswordResetView always shows the
same generic "check your email" success page regardless of whether a
matching user was found - so an SSO-only user requesting a reset was told
it worked and then never received anything, with no hint that their
account has no password at all. SsoAwarePasswordResetForm keeps that
anti-enumeration property (the requester-facing response never reveals
which branch fired) while routing SSO-only accounts to a distinct email
that names their sign-in provider instead of a reset link.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from model_bakery import baker
from social_django.models import UserSocialAuth

from urbanlens.core.tests.testcase import TestCase


class PasswordResetSsoAwarenessTests(TestCase):
    def _make_password_user(self) -> User:
        user = baker.make(User, email="pw-user@example.com", is_active=True)
        user.set_password("correct horse battery staple")  # nosec B106 - test fixture password
        user.save(update_fields=["password"])
        return user

    def _make_sso_only_user(self, provider: str = "google-oauth2") -> User:
        user = baker.make(User, email="sso-user@example.com", is_active=True)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        UserSocialAuth.objects.create(user=user, provider=provider, uid="12345")
        return user

    def test_password_auth_user_gets_the_normal_reset_email(self) -> None:
        self._make_password_user()

        response = self.client.post(reverse("password_reset"), {"email": "pw-user@example.com"})

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "Reset your UrbanLens password")
        self.assertIn("reset/", sent.body)

    def test_sso_only_user_gets_the_sso_notice_email_instead_of_a_reset_link(self) -> None:
        self._make_sso_only_user(provider="google-oauth2")

        response = self.client.post(reverse("password_reset"), {"email": "sso-user@example.com"})

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertNotEqual(sent.subject, "Reset your UrbanLens password")
        self.assertIn("Google", sent.body)
        self.assertNotIn("reset/", sent.body)

    def test_sso_notice_names_discord_when_that_is_the_provider(self) -> None:
        self._make_sso_only_user(provider="discord")

        self.client.post(reverse("password_reset"), {"email": "sso-user@example.com"})

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Discord", mail.outbox[0].body)

    def test_sso_only_user_with_no_social_auth_row_gets_the_generic_hint(self) -> None:
        """A passwordless account with no matching provider row (edge case,
        e.g. the social_auth row was deleted) still gets a helpful email
        rather than crashing - falls back to the generic phrasing."""
        user = baker.make(User, email="orphan@example.com", is_active=True)
        user.set_unusable_password()
        user.save(update_fields=["password"])

        self.client.post(reverse("password_reset"), {"email": "orphan@example.com"})

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("a social account", mail.outbox[0].body)

    def test_response_is_identical_whether_or_not_the_email_matches_anyone(self) -> None:
        """Anti-enumeration: the requester-facing response must not reveal
        whether the address matched a password-auth user, an SSO-only user,
        or nobody at all."""
        self._make_password_user()
        self._make_sso_only_user()

        password_response = self.client.post(reverse("password_reset"), {"email": "pw-user@example.com"})
        sso_response = self.client.post(reverse("password_reset"), {"email": "sso-user@example.com"})
        unknown_response = self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})

        self.assertRedirects(password_response, reverse("password_reset_done"))
        self.assertRedirects(sso_response, reverse("password_reset_done"))
        self.assertRedirects(unknown_response, reverse("password_reset_done"))

    def test_inactive_sso_only_user_receives_no_email(self) -> None:
        """Inactive accounts must stay excluded, matching Django's default
        get_users() behavior for password-auth accounts."""
        user = baker.make(User, email="inactive@example.com", is_active=False)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        UserSocialAuth.objects.create(user=user, provider="google-oauth2", uid="99999")

        self.client.post(reverse("password_reset"), {"email": "inactive@example.com"})

        self.assertEqual(len(mail.outbox), 0)
