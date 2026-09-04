"""Demo account lifecycle: the reserved prefix, and the purge.

The prefix is the only handle either mechanism has. If a real account could
register one, `purge_demo_accounts` would select and destroy it - so reserving
it is a data-safety guard, not tidiness.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.auth.username import username_is_taken
from urbanlens.dashboard.services.demo import DEMO_USERNAME_PREFIX


class ReservedDemoPrefixTests(TestCase):
    def test_the_demo_prefix_is_refused(self) -> None:
        self.assertTrue(username_is_taken("demo-abcd1234-0"))

    def test_a_confusable_spelling_of_the_prefix_is_refused(self) -> None:
        """dem0- normalises onto demo-, so it cannot walk around the guard."""
        self.assertTrue(username_is_taken("dem0-abcd1234-0"))

    def test_an_ordinary_username_is_unaffected(self) -> None:
        self.assertFalse(username_is_taken("democracy_fan"))


class PurgeDemoAccountsTests(TestCase):
    def _demo_user(self, *, age_hours: int) -> User:
        user = baker.make(User, username=f"{DEMO_USERNAME_PREFIX}aaaa1111-0", email="")
        User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(hours=age_hours))
        return user

    def _run(self, **kwargs) -> str:
        out = StringIO()
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", True):
            call_command("purge_demo_accounts", stdout=out, **kwargs)
        return out.getvalue()

    def test_it_refuses_to_run_off_a_demo_instance(self) -> None:
        """The prefix is a weaker guard than a separate database, so require intent."""
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", False), self.assertRaises(CommandError):
            call_command("purge_demo_accounts", execute=True)

    def test_a_dry_run_deletes_nothing(self) -> None:
        user = self._demo_user(age_hours=48)

        output = self._run(ttl_hours=24)

        self.assertIn("would be deleted", output)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_execute_deletes_an_expired_account(self) -> None:
        user = self._demo_user(age_hours=48)

        self._run(ttl_hours=24, execute=True)

        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def test_a_fresh_demo_account_survives(self) -> None:
        user = self._demo_user(age_hours=1)

        self._run(ttl_hours=24, execute=True)

        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_a_real_account_is_never_selected(self) -> None:
        real = baker.make(User, username="genuine_person")
        User.objects.filter(pk=real.pk).update(date_joined=timezone.now() - timedelta(days=365))

        self._run(ttl_hours=24, execute=True)

        self.assertTrue(User.objects.filter(pk=real.pk).exists())


class ScheduledPurgeTaskTests(TestCase):
    """The Celery task wrapper the beat schedule fires unconditionally."""

    def test_it_is_a_noop_off_a_demo_instance(self) -> None:
        from urbanlens.dashboard.tasks import run_scheduled_demo_account_purge

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", False):
            result = run_scheduled_demo_account_purge()

        self.assertFalse(result)

    def test_it_purges_on_a_demo_instance(self) -> None:
        from urbanlens.dashboard.tasks import run_scheduled_demo_account_purge

        user = self._demo_user(age_hours=48)

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", True):
            result = run_scheduled_demo_account_purge()

        self.assertTrue(result)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def _demo_user(self, *, age_hours: int) -> User:
        user = baker.make(User, username=f"{DEMO_USERNAME_PREFIX}bbbb2222-0", email="")
        User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(hours=age_hours))
        return user
