"""Tests for the integration suite's account provisioning.

Two things are worth testing here and they are not the same thing.

The first is that a provisioned account is actually usable by a headless run.
Every precondition in ``services.integration_testing.accounts`` exists because
some redirect, prompt or challenge would otherwise stop the suite before its
first assertion, and each is a single field that a future change could quietly
flip back. A test that only checked "a user row exists" would pass through every
one of those regressions.

The second is the selection query behind ``--purge``. It deletes accounts and
everything hanging off them, and it may be pointed at a staging instance people
also use by hand. Its boundaries are the safety property of this whole feature,
so they are tested from both sides: that it finds what it should, and - more
importantly - that it does not find anything else.
"""

from __future__ import annotations

from io import StringIO
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from urbanlens.dashboard.models.account.model import AccountKdf, ApiKey, ApiKeyScope, EmailVerification, TOTPDevice
from urbanlens.dashboard.models.notifications.meta.delivery_preference import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.site_admin import promote_first_user_if_needed
from urbanlens.dashboard.services.auth.api_keys import authenticate_api_key
from urbanlens.dashboard.services.integration_testing import INTEGRATION_EMAIL_DOMAIN, INTEGRATION_USERNAME_PREFIX
from urbanlens.dashboard.services.integration_testing.accounts import (
    email_for,
    integration_users,
    provision,
    provision_account,
    purge,
    username_for,
)

PASSWORD = "provisioning-test-password"  # noqa: S105 - a fixture value, not a credential


class ProvisionAccountTests(TestCase):
    """What one provisioned account looks like."""

    def test_account_is_named_by_both_conventions(self):
        account, created = provision_account("primary", password=PASSWORD)

        self.assertTrue(created)
        self.assertEqual(account.username, f"{INTEGRATION_USERNAME_PREFIX}primary")
        self.assertTrue(account.email.endswith(f"@{INTEGRATION_EMAIL_DOMAIN}"))

    def test_account_can_actually_sign_in(self):
        """Active, verified, and holding the password that was reported.

        Each of these is separately load-bearing: an inactive account is
        refused outright, an unverified one is refused with an offer to resend
        an email nobody can receive, and a password that does not match the
        manifest makes every sign-in in the suite fail for a reason the suite
        cannot see.
        """
        account, _ = provision_account("primary", password=PASSWORD)

        user = User.objects.get(username=account.username)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertIsNotNone(EmailVerification.objects.get(user=user).verified_at)

    def test_profile_is_past_every_post_login_diversion(self):
        """``PostLoginRedirectView`` must land the run in the application.

        Without both flags it redirects to the welcome flow or to profile
        editing, and every navigation the suite makes afterwards is against the
        wrong page.
        """
        account, _ = provision_account("primary", password=PASSWORD)

        profile = Profile.objects.get(user__username=account.username)
        self.assertTrue(profile.welcome_onboarding_complete)
        self.assertTrue(profile.profile_setup_complete)

    def test_outbound_providers_are_off_by_default(self):
        account, _ = provision_account("primary", password=PASSWORD)

        profile = Profile.objects.get(user__username=account.username)
        self.assertFalse(profile.external_apis_enabled)
        self.assertFalse(profile.ai_enabled)

    def test_outbound_providers_can_be_left_on_deliberately(self):
        account, _ = provision_account("primary", password=PASSWORD, external_apis=True)

        profile = Profile.objects.get(user__username=account.username)
        self.assertTrue(profile.external_apis_enabled)
        self.assertTrue(profile.ai_enabled)

    def test_no_notification_is_ever_delivered_by_email(self):
        """Every delivery preference is on-site only.

        The address is on a reserved domain that cannot receive mail, so an
        email preference would produce a delivery failure inside whatever task
        raised the notification - reported as that feature failing.
        """
        account, _ = provision_account("primary", password=PASSWORD)

        preferences = NotificationPreference.objects.get(profile__user__username=account.username)
        email_carrying = {DeliveryPreference.EMAIL, DeliveryPreference.BOTH}
        offenders = [
            field.name
            for field in NotificationPreference._meta.get_fields()
            if getattr(field, "choices", None)
            and {value for value, _ in field.choices} == set(DeliveryPreference.values)
            and getattr(preferences, field.name) in email_carrying
        ]
        self.assertEqual(offenders, [], f"these notification types would still send email: {offenders}")

    def test_second_factors_are_cleared(self):
        """A challenge a headless run cannot answer must not survive provisioning."""
        user = User.objects.create_user(username=username_for("primary"), email=email_for("primary"))
        TOTPDevice.objects.create(user=user, secret="ABCDEFGHIJKLMNOP")  # noqa: S106 - a fixture value being cleared, not a credential
        AccountKdf.objects.set_auth_salt(user, "c29tZS1zYWx0LXZhbHVl")

        provision_account("primary", password=PASSWORD)

        self.assertFalse(TOTPDevice.objects.filter(user=user).exists())
        self.assertFalse(AccountKdf.objects.filter(user=user).exists())


class ApiKeyProvisioningTests(TestCase):
    """The two keys, and why there are two."""

    def test_the_main_key_authenticates_and_holds_every_scope(self):
        account, _ = provision_account("primary", password=PASSWORD)

        assert account.api_key is not None
        resolved = authenticate_api_key(account.api_key)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(set(resolved.scopes), {scope.value for scope in ApiKeyScope})

    def test_the_restricted_key_is_valid_and_insufficient(self):
        """Valid credential, minimal grant.

        A key that does not authenticate proves nothing about scope
        enforcement - the endpoint would refuse it at the authentication step
        and the test would pass whether or not scopes were checked at all.
        """
        account, _ = provision_account("primary", password=PASSWORD)

        assert account.restricted_api_key is not None
        resolved = authenticate_api_key(account.restricted_api_key)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.scopes, [ApiKeyScope.PROFILE_READ.value])

    def test_reprovisioning_revokes_the_keys_it_replaces(self):
        first, _ = provision_account("primary", password=PASSWORD)
        assert first.api_key is not None

        second, _ = provision_account("primary", password=PASSWORD)

        self.assertIsNone(authenticate_api_key(first.api_key), "a key from a previous run still authenticates")
        assert second.api_key is not None
        self.assertIsNotNone(authenticate_api_key(second.api_key))

    def test_keys_can_be_skipped(self):
        account, _ = provision_account("primary", password=PASSWORD, with_api_keys=False)

        self.assertIsNone(account.api_key)
        self.assertFalse(ApiKey.objects.filter(user__username=account.username).exists())


class IdempotencyTests(TestCase):
    """Re-running provisioning refreshes rather than accumulates."""

    def test_a_second_run_reuses_the_same_account(self):
        provision_account("primary", password=PASSWORD)
        _, created = provision_account("primary", password="a-different-password")  # noqa: S106 - fixture value

        self.assertFalse(created)
        self.assertEqual(User.objects.filter(username=username_for("primary")).count(), 1)

    def test_a_second_run_resets_the_password(self):
        provision_account("primary", password=PASSWORD)
        provision_account("primary", password="a-different-password")  # noqa: S106 - fixture value

        user = User.objects.get(username=username_for("primary"))
        self.assertTrue(user.check_password("a-different-password"))

    def test_every_role_shares_one_password(self):
        """One password for the run, so a manifest is readable and a shell export is short."""
        result = provision(["primary", "secondary"])

        passwords = {account.password for account in result.accounts}
        self.assertEqual(len(passwords), 1)
        self.assertEqual(len(result.accounts), 2)


class SelectionBoundaryTests(TestCase):
    """What ``--purge`` is and is not allowed to see.

    The negative cases matter more than the positive one. This query deletes
    accounts and everything hanging off them, and it may be run on an instance
    that also holds accounts somebody is using.
    """

    def test_a_provisioned_account_is_selected(self):
        provision_account("primary", password=PASSWORD)

        self.assertEqual([user.username for user in integration_users()], [username_for("primary")])

    def test_a_real_account_is_not_selected(self):
        User.objects.create_user(username="a_real_person", email="someone@example.com")

        self.assertEqual(list(integration_users()), [])

    def test_the_username_prefix_alone_is_not_enough(self):
        """Both conventions are required, so one of them being guessed is not sufficient."""
        User.objects.create_user(username=username_for("impostor"), email="someone@example.com")

        self.assertEqual(list(integration_users()), [])

    def test_the_email_domain_alone_is_not_enough(self):
        User.objects.create_user(username="a_real_person", email=f"a_real_person@{INTEGRATION_EMAIL_DOMAIN}")

        self.assertEqual(list(integration_users()), [])

    def test_a_staff_account_is_never_selected(self):
        """A staff account carrying both conventions is still excluded.

        The last line of defence: if somebody promotes one of these to
        investigate something, a later purge must not silently take the
        elevated account with it.
        """
        User.objects.create_user(username=username_for("primary"), email=email_for("primary"), is_staff=True)

        self.assertEqual(list(integration_users()), [])

    def test_purge_removes_only_provisioned_accounts(self):
        provision_account("primary", password=PASSWORD)
        provision_account("secondary", password=PASSWORD)
        User.objects.create_user(username="a_real_person", email="someone@example.com")

        deleted = purge()

        self.assertEqual(sorted(deleted), sorted([username_for("primary"), username_for("secondary")]))
        self.assertTrue(User.objects.filter(username="a_real_person").exists())
        self.assertEqual(list(integration_users()), [])


class BootstrapAdminGuardTests(TestCase):
    """A disposable account must never claim the single, permanent admin slot.

    Asserted against ``SiteSettings.bootstrap_admin_user``, which is the
    authoritative record, rather than against the return value: the promotion
    runs from a ``post_save`` signal, so by the time a test can call the
    function itself the decision has already been made once.
    """

    def setUp(self):
        """Establish the global state these tests read, rather than assuming it.

        ``promote_first_user_if_needed`` consults two pieces of site-wide state:
        the ``SiteSettings`` bootstrap slot, and whether any ``User`` other than
        the one being created exists. Neither is this file's to assume. Against a
        database another file has already written to - which is what
        ``bin/run_tests.sh --fast`` reuses - all three tests here fail on somebody
        else's fixture, and read as a regression in whatever was being worked on.

        Clearing the slot alone would not be enough: the "an ordinary first user
        is still promoted" case needs an empty user table as well. Both writes are
        inside the test's transaction and roll back with it.
        """
        super().setUp()
        User.objects.all().delete()
        # Redundant while the FK is SET_NULL, but this is the value every
        # assertion below reads, so it is established rather than inferred.
        SiteSettings.objects.filter(pk=1).update(bootstrap_admin_user=None)

    def _bootstrap_admin_id(self) -> int | None:
        return SiteSettings.get_current().bootstrap_admin_user_id

    def test_the_first_provisioned_account_is_not_promoted(self):
        """Provisioning against a freshly built database creates the first user on it.

        The slot is single-claim and permanent, so a throwaway account taking
        it would leave the real operator unable to ever be promoted - and a
        purge would then leave it pointing at a row that no longer exists.
        """
        User.objects.create_user(username=username_for("primary"), email=email_for("primary"))

        self.assertIsNone(self._bootstrap_admin_id(), "a disposable account claimed the bootstrap admin slot")

    def test_an_ordinary_first_user_is_still_promoted(self):
        """The guard narrows the rule rather than removing it."""
        user = User.objects.create_user(username="the_operator", email="operator@example.com")

        self.assertEqual(self._bootstrap_admin_id(), user.pk)

    def test_provisioning_leaves_the_slot_unclaimed(self):
        """The order that matters: disposable accounts created on a fresh instance.

        The slot stays empty rather than pointing at an account a purge will
        delete. It does *not* become claimable by whoever signs up next -
        ``promote_first_user_if_needed`` only ever promotes a genuinely first
        user - so on an instance provisioned before anyone registered, the
        operator is promoted deliberately (``createsuperuser``, or the site
        admin group) rather than automatically. That is the recoverable
        outcome; a dangling reference to a deleted row is not.
        """
        provision_account("primary", password=PASSWORD)
        provision_account("secondary", password=PASSWORD)

        self.assertIsNone(self._bootstrap_admin_id())

        operator = User.objects.create_user(username="the_operator", email="operator@example.com")
        self.assertFalse(
            promote_first_user_if_needed(operator),
            "the operator was not the first user, so nothing should have been promoted",
        )
        self.assertIsNone(self._bootstrap_admin_id())


class CommandTests(TestCase):
    """The management command's own behaviour: output, and the production locks."""

    def test_json_output_is_a_manifest_the_runner_can_read(self):
        out = StringIO()
        call_command("provision_integration_env", "--roles", "primary,secondary", stdout=out)

        manifest = json.loads(out.getvalue())
        self.assertEqual({account["role"] for account in manifest["accounts"]}, {"primary", "secondary"})
        for account in manifest["accounts"]:
            # The TypeScript loader reads exactly these keys; a rename here
            # produces a run that cannot sign in and says only "no accounts".
            for key in ("role", "username", "password", "api_key", "scopes", "restricted_api_key"):
                self.assertIn(key, account)

    def test_text_output_is_shell_pasteable(self):
        out = StringIO()
        call_command("provision_integration_env", "--roles", "primary", "--format", "text", stdout=out)

        self.assertIn("export UL_E2E_USERNAME=", out.getvalue())
        self.assertIn("export UL_E2E_PASSWORD=", out.getvalue())

    def test_purge_is_a_dry_run_without_execute(self):
        provision_account("primary", password=PASSWORD)
        out = StringIO()

        call_command("provision_integration_env", "--purge", stdout=out)

        self.assertIn("would be deleted", out.getvalue())
        self.assertEqual(len(list(integration_users())), 1)

    def test_purge_deletes_with_execute(self):
        provision_account("primary", password=PASSWORD)

        call_command("provision_integration_env", "--purge", "--execute", stdout=StringIO())

        self.assertEqual(list(integration_users()), [])

    def test_production_is_refused(self):
        with mock.patch("urbanlens.dashboard.management.commands.provision_integration_env.app_settings") as settings:
            settings.environment_name = "production"
            with self.assertRaises(CommandError) as caught:
                call_command("provision_integration_env", stdout=StringIO())

        self.assertIn("production", str(caught.exception))
        self.assertFalse(User.objects.filter(username=username_for("primary")).exists())

    def test_force_alone_does_not_open_production(self):
        """Two locks, because each covers a different mistake."""
        with (
            mock.patch("urbanlens.dashboard.management.commands.provision_integration_env.app_settings") as settings,
            mock.patch.dict("os.environ", {}, clear=False),
        ):
            settings.environment_name = "production"
            with self.assertRaises(CommandError):
                call_command("provision_integration_env", "--force", stdout=StringIO())

    def test_both_locks_together_permit_production(self):
        with (
            mock.patch("urbanlens.dashboard.management.commands.provision_integration_env.app_settings") as settings,
            mock.patch.dict("os.environ", {"UL_ALLOW_INTEGRATION_PROVISIONING": "true"}),
        ):
            settings.environment_name = "production"
            call_command(
                "provision_integration_env", "--force", "--roles", "primary", stdout=StringIO(), stderr=StringIO()
            )

        self.assertTrue(User.objects.filter(username=username_for("primary")).exists())
