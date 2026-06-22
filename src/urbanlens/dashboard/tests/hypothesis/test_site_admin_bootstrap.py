"""Tests for first-user site admin bootstrap."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.site_admin import (
    SITE_ADMIN_GROUP_NAME,
    complete_site_admin_onboarding,
    promote_first_user_if_needed,
    should_redirect_to_site_admin,
)


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


class SiteAdminRedirectTests(TestCase):
    """Bootstrap admin is redirected to site admin once after first login."""

    def setUp(self) -> None:
        self.user: User = baker.make(User, username="founder")
        Group.objects.get_or_create(name=SITE_ADMIN_GROUP_NAME)[0].user_set.add(self.user)
        settings = SiteSettings.get_current()
        settings.bootstrap_admin_user = self.user
        settings.bootstrap_admin_onboarding_complete = False
        settings.save()

    def test_should_redirect_before_onboarding(self) -> None:
        self.assertTrue(should_redirect_to_site_admin(self.user))

    def test_should_not_redirect_after_onboarding(self) -> None:
        complete_site_admin_onboarding(self.user)
        self.assertFalse(should_redirect_to_site_admin(self.user))

    def test_post_login_redirects_bootstrap_admin_to_site_admin(self) -> None:
        client = Client()
        client.force_login(self.user)

        response = client.get(reverse("post_login"), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("site_admin"))

    def test_post_login_redirects_to_map_after_onboarding(self) -> None:
        complete_site_admin_onboarding(self.user)
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
