"""Smoke: the demo seeder runs against the real models and produces a usable account."""
from __future__ import annotations

from unittest import mock

from django import test as django_test
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


class SeedingCommitOrderingTests(django_test.TransactionTestCase):
    """The Celery patch has to survive past the actual transaction commit.

    `TransactionTestCase`, not `TestCase`, and deliberately: `TestCase` wraps
    every test in a savepoint that is rolled back, so Django's real commit
    machinery (`connection.run_and_clear_commit_hooks`) never runs and
    `transaction.on_commit` callbacks never fire on their own -
    `captureOnCommitCallbacks(execute=True)` papers over that by deferring
    every captured callback to when the *test's own* `with` block exits, which
    is after `seed_demo_account()` has already returned either way. That
    cannot tell a patch that outlives the real commit from one that merely
    outlives the function call, which is exactly the distinction this test
    exists to make - so it needs a real commit, which only
    `TransactionTestCase` gives it.

    Pin creation fires `ensure_wiki_for_pin_location` on post_save, which
    defers to `transaction.on_commit` rather than calling `safely_enqueue_task`
    immediately - unconditionally (just `created` and a `community_enabled`
    profile, both true for every demo pin), so unlike an achievement-evaluation
    trigger it needs no Achievement fixture data to actually fire.

    The tripwire patches `apply_async` on the real task, one layer below the
    `safely_enqueue_task` seam that `seed_demo_account` itself patches - if the
    ordering bug were reintroduced, seeding's own patch would already have
    exited by the time the real commit fires the callback, and the callback's
    *fresh* import of `safely_enqueue_task` would resolve to the real
    function, which calls exactly this.
    """

    def test_a_deferred_pin_wiki_enqueue_never_reaches_the_real_dispatcher(self) -> None:
        from model_bakery import baker

        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.tasks import ensure_wiki_for_location

        # The location pool is empty by default (no manifest configured in
        # tests), so no Pin - and no signal - would fire without this: give
        # the seeder one real Location to actually pin.
        location = baker.make(Location, google_place=None)
        with mock.patch("urbanlens.dashboard.services.demo.seeding.pool_locations", return_value=[location]), mock.patch.object(ensure_wiki_for_location, "apply_async") as apply_async:
            seed_demo_account()

        apply_async.assert_not_called()
