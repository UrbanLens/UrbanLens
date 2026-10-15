"""Tests for first-user site admin bootstrap."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import (
    SITE_ADMIN_GROUP_NAME,
    add_user_to_site_admin_group,
    complete_site_admin_onboarding,
    promote_first_user_if_needed,
    should_redirect_to_site_admin,
)
from urbanlens.UrbanLens.settings.app import settings as app_settings


class PromoteFirstUserTests(TestCase):
    """First user on a fresh site becomes bootstrap site admin."""

    def test_first_user_is_promoted_to_site_admin(self) -> None:
        user: User = baker.make(User, username="founder")

        settings = SiteSettings.get_current()
        self.assertEqual(settings.bootstrap_admin_user_id, user.pk)
        self.assertFalse(settings.bootstrap_admin_onboarding_complete)
        self.assertTrue(user.groups.filter(name=SITE_ADMIN_GROUP_NAME).exists())

    def test_second_user_is_not_promoted(self) -> None:
        baker.make(User, username="founder")
        second: User = baker.make(User, username="member")

        self.assertFalse(promote_first_user_if_needed(second))
        self.assertFalse(second.groups.filter(name=SITE_ADMIN_GROUP_NAME).exists())

    def test_promote_is_idempotent_for_first_user(self) -> None:
        user: User = baker.make(User, username="founder")
        self.assertFalse(promote_first_user_if_needed(user))

    def test_bootstrap_email_unset_leaves_the_first_account_in_charge(self) -> None:
        with mock.patch.object(app_settings, "bootstrap_admin_email", ""):
            user: User = baker.make(User, username="founder", email="anyone@example.com")

        self.assertEqual(SiteSettings.get_current().bootstrap_admin_user_id, user.pk)

    def test_bootstrap_email_matches_case_insensitively(self) -> None:
        with mock.patch.object(app_settings, "bootstrap_admin_email", "Owner@Example.com"):
            user: User = baker.make(User, username="founder", email="owner@example.COM")

        self.assertEqual(SiteSettings.get_current().bootstrap_admin_user_id, user.pk)

    def test_bootstrap_email_matches_after_gmail_normalization(self) -> None:
        with mock.patch.object(app_settings, "bootstrap_admin_email", "jake.smith@gmail.com"):
            user: User = baker.make(User, username="founder", email="JakeSmith+urbex@googlemail.com")

        self.assertEqual(SiteSettings.get_current().bootstrap_admin_user_id, user.pk)

    def test_first_account_with_another_email_does_not_claim_the_slot(self) -> None:
        with mock.patch.object(app_settings, "bootstrap_admin_email", "owner@example.com"):
            stranger: User = baker.make(User, username="stranger", email="stranger@example.com")

        settings = SiteSettings.get_current()
        self.assertIsNone(settings.bootstrap_admin_user_id)
        self.assertFalse(stranger.groups.filter(name=SITE_ADMIN_GROUP_NAME).exists())

    def test_non_matching_account_is_refused_even_when_the_slot_is_empty(self) -> None:
        with mock.patch.object(app_settings, "bootstrap_admin_email", "owner@example.com"):
            self.assertFalse(promote_first_user_if_needed(baker.make(User, username="guest", email="")))

    def test_matching_account_claims_the_empty_slot_after_others_exist(self) -> None:
        with mock.patch.object(app_settings, "bootstrap_admin_email", "owner@example.com"):
            baker.make(User, username="early", email="early@example.com")
            owner: User = baker.make(User, username="owner", email="Owner@Example.com")

        self.assertEqual(SiteSettings.get_current().bootstrap_admin_user_id, owner.pk)
        self.assertTrue(owner.groups.filter(name=SITE_ADMIN_GROUP_NAME).exists())

    def test_matching_account_cannot_displace_a_claimed_slot(self) -> None:
        first: User = baker.make(User, username="founder", email="founder@example.com")
        with mock.patch.object(app_settings, "bootstrap_admin_email", "owner@example.com"):
            owner: User = baker.make(User, username="owner", email="owner@example.com")

        self.assertEqual(SiteSettings.get_current().bootstrap_admin_user_id, first.pk)
        self.assertFalse(owner.groups.filter(name=SITE_ADMIN_GROUP_NAME).exists())


class SiteAdminRedirectTests(TestCase):
    """Bootstrap admin is redirected to site admin once after first login."""

    def setUp(self) -> None:
        self.user: User = baker.make(User, username="founder")
        add_user_to_site_admin_group(self.user)
        settings = SiteSettings.get_current()
        settings.bootstrap_admin_user = self.user
        settings.bootstrap_admin_onboarding_complete = False
        settings.save()

    def test_should_redirect_before_onboarding(self) -> None:
        self.assertTrue(should_redirect_to_site_admin(self.user))

    def test_should_not_redirect_after_onboarding(self) -> None:
        complete_site_admin_onboarding(self.user)
        self.assertFalse(should_redirect_to_site_admin(self.user))

    def test_post_login_redirects_bootstrap_admin_to_setup(self) -> None:
        client = Client()
        client.force_login(self.user)

        response = client.get(reverse("post_login"), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("setup"))

    def test_post_login_redirects_to_map_after_onboarding(self) -> None:
        complete_site_admin_onboarding(self.user)
        # Isolate the site-admin redirect chain from the separate welcome/
        # profile-setup chain (covered by WelcomeRedirectChainTests).
        Profile.objects.filter(user=self.user).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        client = Client()
        client.force_login(self.user)

        response = client.get(reverse("post_login"), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("map.view"))

    def test_site_admin_get_marks_onboarding_complete(self) -> None:
        client = Client()
        client.force_login(self.user)

        response = client.get(reverse("site_admin"))

        self.assertEqual(response.status_code, 200)
        settings = SiteSettings.get_current()
        self.assertTrue(settings.bootstrap_admin_onboarding_complete)
