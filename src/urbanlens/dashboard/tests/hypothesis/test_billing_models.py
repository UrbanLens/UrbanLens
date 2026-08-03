"""Tests for the billing models package: SubscriptionRole pricing validation,
RoleSubscription's unique-active-per-role constraint, and granting_access_for()."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole


class SubscriptionRoleCleanTests(TestCase):
    """SubscriptionRole.clean() enforces the pricing-field combinations."""

    def test_plain_role_is_valid(self) -> None:
        role = baker.prepare(SubscriptionRole, pay_what_you_want=False, pwyw_dynamic_threshold=False, pwyw_minimum_cents=None)
        role.clean()

    def test_dynamic_threshold_requires_pay_what_you_want(self) -> None:
        role = baker.prepare(SubscriptionRole, pay_what_you_want=False, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None)
        with pytest.raises(ValidationError) as ctx:
            role.clean()
        self.assertIn("pwyw_dynamic_threshold", ctx.value.message_dict)

    def test_dynamic_threshold_cannot_combine_with_a_static_minimum(self) -> None:
        role = baker.prepare(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=300)
        with pytest.raises(ValidationError) as ctx:
            role.clean()
        self.assertIn("pwyw_minimum_cents", ctx.value.message_dict)

    def test_plain_pwyw_with_a_static_minimum_is_valid(self) -> None:
        role = baker.prepare(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=300)
        role.clean()

    def test_dynamic_threshold_alone_is_valid(self) -> None:
        role = baker.prepare(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None)
        role.clean()


class SubscriptionRoleIsPurchasableTests(TestCase):
    def test_neither_fixed_price_nor_pwyw_is_not_purchasable(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=False)
        self.assertFalse(role.is_purchasable)

    def test_fixed_price_alone_is_purchasable(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=500, pay_what_you_want=False)
        self.assertTrue(role.is_purchasable)

    def test_pwyw_alone_is_purchasable(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=True)
        self.assertTrue(role.is_purchasable)


class RoleSubscriptionUniqueConstraintTests(TestCase):
    """One non-canceled RoleSubscription per (user, role)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def test_duplicate_active_subscription_raises_integrity_error(self) -> None:
        baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE)
        with pytest.raises(IntegrityError), transaction.atomic():
            baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.TRIALING)

    def test_a_canceled_subscription_does_not_block_a_new_one(self) -> None:
        baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.CANCELED)
        try:
            baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE)
        except IntegrityError as exc:
            self.fail(f"A canceled subscription should not conflict with a new one: {exc}")


class RoleSubscriptionGrantingAccessForTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def test_active_and_threshold_met_grants_access(self) -> None:
        sub = baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE, threshold_met=True)
        self.assertIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_trialing_and_threshold_met_grants_access(self) -> None:
        sub = baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.TRIALING, threshold_met=True)
        self.assertIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_active_but_threshold_not_met_does_not_grant_access(self) -> None:
        sub = baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.ACTIVE, threshold_met=False)
        self.assertNotIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_canceled_does_not_grant_access_even_if_threshold_met(self) -> None:
        sub = baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.CANCELED, threshold_met=True)
        self.assertNotIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_past_due_does_not_grant_access(self) -> None:
        sub = baker.make(RoleSubscription, user=self.user, role=self.role, status=BillingSubscriptionStatus.PAST_DUE, threshold_met=True)
        self.assertNotIn(sub, RoleSubscription.objects.granting_access_for(self.user))
