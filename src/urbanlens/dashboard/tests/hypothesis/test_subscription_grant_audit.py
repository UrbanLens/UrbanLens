"""A grant outlives the admin who made it (P246), and records who revoked it (P245)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import (
    SubscriptionRole,
    UserSubscription,
    active_subscription_roles,
    grant_subscription,
)
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group
from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile

_URL = reverse("site_admin_subscriptions")


def _admin(username: str) -> User:
    user = baker.make(User, username=username)
    add_user_to_site_admin_group(user)
    Profile.objects.filter(user=user).update(welcome_onboarding_complete=True, profile_setup_complete=True)
    return user


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


class _GrantFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.granter = _admin("admin-granter")
        self.other_admin = _admin("admin-other")
        self.role = baker.make(SubscriptionRole, slug="explorer", name="Explorer", features="")
        self.grantee = baker.make(User, username="grantee-gina")
        self.grant = grant_subscription(self.grantee, self.role, self.granter, None)


class DeletedGranterTests(_GrantFixture):
    def test_the_grant_survives_its_granter_being_deleted(self) -> None:
        hard_delete_profile(self.granter.profile)

        self.grant.refresh_from_db()
        self.assertIsNone(self.grant.granted_by)
        self.assertIsNone(self.grant.revoked_at)
        self.assertIn(self.role, active_subscription_roles(self.grantee))

    def test_the_grant_list_names_a_deleted_granter(self) -> None:
        hard_delete_profile(self.granter.profile)

        response = _client_for(self.other_admin).get(_URL)

        self.assertContains(response, "grantee-gina")
        self.assertContains(response, "Deleted account")

    def test_another_admin_can_still_revoke_it(self) -> None:
        hard_delete_profile(self.granter.profile)

        _client_for(self.other_admin).post(_URL, {"action": "revoke", "subscription_id": self.grant.pk})

        self.grant.refresh_from_db()
        self.assertIsNotNone(self.grant.revoked_at)


class RevokedByTests(_GrantFixture):
    def test_revoking_records_who_revoked(self) -> None:
        _client_for(self.other_admin).post(_URL, {"action": "revoke", "subscription_id": self.grant.pk})

        self.grant.refresh_from_db()
        self.assertEqual(self.grant.revoked_by, self.other_admin)

    def test_a_second_revoke_keeps_the_first_revoker(self) -> None:
        _client_for(self.other_admin).post(_URL, {"action": "revoke", "subscription_id": self.grant.pk})
        _client_for(self.granter).post(_URL, {"action": "revoke", "subscription_id": self.grant.pk})

        self.grant.refresh_from_db()
        self.assertEqual(self.grant.revoked_by, self.other_admin)

    def test_the_model_revoke_records_its_revoker(self) -> None:
        self.grant.revoke(by=self.other_admin)

        self.grant.refresh_from_db()
        self.assertEqual(self.grant.revoked_by, self.other_admin)

    def test_the_revoked_grant_survives_its_revoker_being_deleted(self) -> None:
        self.grant.revoke(by=self.other_admin)

        hard_delete_profile(self.other_admin.profile)

        self.grant.refresh_from_db()
        self.assertIsNone(self.grant.revoked_by)
        self.assertIsNotNone(self.grant.revoked_at)

    def test_regranting_clears_the_revoker(self) -> None:
        self.grant.revoke(by=self.other_admin)

        regrant = grant_subscription(self.grantee, self.role, self.granter, None)

        self.assertIsNone(regrant.revoked_by)
        self.assertEqual(UserSubscription.objects.not_revoked().filter(user=self.grantee).count(), 1)
