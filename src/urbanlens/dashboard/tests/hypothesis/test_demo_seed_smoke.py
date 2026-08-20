"""Smoke: the demo seeder runs against the real models and produces a usable account."""
from __future__ import annotations

from django.contrib.auth.models import User

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.demo import DEMO_USERNAME_PREFIX
from urbanlens.dashboard.services.demo.seeding import seed_demo_account


class DemoSeedSmokeTests(TestCase):
    def test_it_creates_a_usable_login_account(self) -> None:
        user = seed_demo_account()

        self.assertTrue(user.username.startswith(DEMO_USERNAME_PREFIX))
        self.assertTrue(user.is_active)
        self.assertEqual(user.email, "")
        profile = user.profile
        self.assertFalse(profile.external_apis_enabled)
        self.assertTrue(profile.profile_setup_complete)

    def test_it_creates_the_login_account_and_its_personas(self) -> None:
        seed_demo_account()

        demo_users = User.objects.filter(username__startswith=DEMO_USERNAME_PREFIX)
        self.assertEqual(demo_users.count(), 5, "one login account plus four personas")

    def test_an_empty_pool_seeds_no_pins_rather_than_inventing_any(self) -> None:
        """No manifest is the state of a demo instance before anything is imported.

        A pin at an invented coordinate has no real place behind it, so its
        detail page resolves no boundary, no parcel and no wiki - the product
        looking broken. Seeding nothing is the honest outcome.
        """
        user = seed_demo_account()

        self.assertEqual(Pin.objects.filter(profile=user.profile).count(), 0)

    def test_two_sessions_do_not_collide(self) -> None:
        first = seed_demo_account()
        second = seed_demo_account()
        self.assertNotEqual(first.username, second.username)

    def test_a_demo_account_never_becomes_site_admin(self) -> None:
        """On a fresh demo database the first visitor is the first user.

        The bootstrap-admin slot is single-claim and permanent, so letting a
        throwaway account take it hands it the admin panel and leaves the real
        operator unable to ever be promoted.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings

        user = seed_demo_account()

        self.assertFalse(user.is_staff)
        self.assertFalse(user.groups.filter(name="site_admin").exists())
        settings = SiteSettings.objects.filter(pk=1).first()
        if settings is not None:
            self.assertNotEqual(settings.bootstrap_admin_user_id, user.pk)
