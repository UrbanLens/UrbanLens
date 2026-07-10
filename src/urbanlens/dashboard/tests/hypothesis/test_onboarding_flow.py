"""Tests for the signup-race fix and the /welcome/ onboarding redirect chain.

Regression coverage for the bug where profile_setup_complete never flipped to
False for normal email signups: the User post_save signal (signals.py) now
sets it explicitly in defaults=, instead of VerifyEmailView relying on a
Profile.objects.get_or_create(...).created check that always came back False
because the signal had already created the row.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.account import PostLoginRedirectView
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.profile.model import Profile


class SignupProfileSetupCompleteTests(TestCase):
    """create_user_profile (the User post_save signal) sets profile_setup_complete=False."""

    def test_email_signup_sets_profile_setup_complete_false(self) -> None:
        response = self.client.post(
            reverse("signup"),
            {
                "username": "newexplorer",
                "email": "newexplorer@example.com",
                "password1": "correct horse battery staple 9",
                "password2": "correct horse battery staple 9",
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="newexplorer")
        # The signal already ran synchronously inside form.save() above -
        # profile_setup_complete must be False immediately, before any
        # email-verification step ever runs.
        self.assertFalse(user.profile.profile_setup_complete)

    def test_email_signup_sets_welcome_onboarding_complete_false(self) -> None:
        self.client.post(
            reverse("signup"),
            {
                "username": "anotherexplorer",
                "email": "anotherexplorer@example.com",
                "password1": "correct horse battery staple 9",
                "password2": "correct horse battery staple 9",
            },
        )
        user = User.objects.get(username="anotherexplorer")
        self.assertFalse(user.profile.welcome_onboarding_complete)

    def test_verify_email_view_does_not_flip_profile_setup_complete_back_to_true(self) -> None:
        user: User = baker.make(User, is_active=False)
        self.assertFalse(user.profile.profile_setup_complete)
        verification: EmailVerification = baker.make(EmailVerification, user=user)

        response = self.client.get(reverse("verify_email", args=[str(verification.token)]))

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.profile.profile_setup_complete)

    def test_existing_user_login_does_not_reset_profile_setup_complete(self) -> None:
        user: User = baker.make(User)
        Profile.objects.filter(pk=user.profile.pk).update(profile_setup_complete=True, welcome_onboarding_complete=True)
        user.refresh_from_db()
        self.assertTrue(user.profile.profile_setup_complete)


class WelcomeRedirectChainTests(TestCase):
    """PostLoginRedirectView routes through /welcome/ before the map, exactly once per profile."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        # A prior user absorbs the first-user site-admin bootstrap promotion
        # (see promote_first_user_if_needed) so the users under test here
        # exercise only the welcome/profile-setup redirect chain.
        baker.make(User)

    def _get_redirect(self, user: User) -> str:
        request = self.factory.get(reverse("post_login"))
        request.user = user
        response = PostLoginRedirectView.as_view()(request)
        self.assertEqual(response.status_code, 302)
        return response["Location"]

    def test_new_user_redirects_to_welcome_page(self) -> None:
        user: User = baker.make(User)
        self.assertFalse(user.profile.welcome_onboarding_complete)
        self.assertEqual(self._get_redirect(user), reverse("onboarding.welcome"))

    def test_existing_user_skips_welcome_page(self) -> None:
        user: User = baker.make(User)
        Profile.objects.filter(pk=user.profile.pk).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        user.refresh_from_db()
        self.assertEqual(self._get_redirect(user), reverse("map.view"))

    def test_welcome_complete_but_profile_setup_incomplete_goes_to_profile_edit(self) -> None:
        user: User = baker.make(User)
        Profile.objects.filter(pk=user.profile.pk).update(welcome_onboarding_complete=True, profile_setup_complete=False)
        user.refresh_from_db()
        self.assertEqual(self._get_redirect(user), reverse("profile.edit"))
