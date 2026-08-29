"""Tests for the billing models package: SubscriptionRole pricing validation,
RoleSubscription's unique-active-per-role constraint, and granting_access_for()."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
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

    def test_dynamic_threshold_with_a_zero_minimum_is_valid(self) -> None:
        """clean() checks `pwyw_minimum_cents` truthily, matching pwyw_minimum_dollars/is_purchasable
        elsewhere treating 0 the same as unset - a switch to an `is not None` check would wrongly
        reject this combination."""
        role = baker.prepare(SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=0)
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

    def test_zero_monthly_price_is_still_purchasable(self) -> None:
        """is_purchasable checks `monthly_price_cents is not None`, not truthiness - an explicit
        $0 price (e.g. a promotional free tier) is a set price, unlike a blank/unset one."""
        role = baker.make(SubscriptionRole, monthly_price_cents=0, pay_what_you_want=False)
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

    def test_canceled_with_unexpired_banked_access_grants_access(self) -> None:
        sub = baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.CANCELED,
            threshold_met=False,
            usage_covered_until=timezone.now() + timedelta(days=1),
        )
        self.assertTrue(sub.grants_access)
        self.assertIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_canceled_with_expired_banked_access_does_not_grant_access(self) -> None:
        sub = baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.CANCELED,
            threshold_met=False,
            usage_covered_until=timezone.now() - timedelta(days=1),
        )
        self.assertFalse(sub.grants_access)
        self.assertNotIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_active_but_under_threshold_with_unexpired_banked_access_grants_access(self) -> None:
        """Banked runway also covers a still-active subscriber whose pledge is currently
        under a dynamic threshold - being "still paying but under threshold" shouldn't be
        worse off than being fully canceled."""
        sub = baker.make(
            RoleSubscription,
            user=self.user,
            role=self.role,
            status=BillingSubscriptionStatus.ACTIVE,
            threshold_met=False,
            usage_covered_until=timezone.now() + timedelta(days=1),
        )
        self.assertTrue(sub.grants_access)
        self.assertIn(sub, RoleSubscription.objects.granting_access_for(self.user))


class RoleSubscriptionBankedAccessBoundaryTests(TestCase):
    """Both grants_access and the queryset filter use a strict `usage_covered_until > now`
    comparison - pin the exact cutoff instant itself (not just a value safely on either side)
    so a `>` -> `>=` regression (or vice versa) is caught. Time is frozen so the read inside
    grants_access/currently_granting can't drift past the instant the row was written with."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole)

    def test_banked_access_excludes_the_exact_cutoff_instant(self) -> None:
        frozen_now = timezone.now()
        with mock.patch("django.utils.timezone.now", return_value=frozen_now):
            sub = baker.make(
                RoleSubscription,
                user=self.user,
                role=self.role,
                status=BillingSubscriptionStatus.CANCELED,
                threshold_met=False,
                usage_covered_until=frozen_now,
            )
            self.assertFalse(sub.grants_access)
            self.assertNotIn(sub, RoleSubscription.objects.granting_access_for(self.user))

    def test_banked_access_grants_one_microsecond_past_the_cutoff(self) -> None:
        frozen_now = timezone.now()
        with mock.patch("django.utils.timezone.now", return_value=frozen_now):
            sub = baker.make(
                RoleSubscription,
                user=self.user,
                role=self.role,
                status=BillingSubscriptionStatus.CANCELED,
                threshold_met=False,
                usage_covered_until=frozen_now + timedelta(microseconds=1),
            )
            self.assertTrue(sub.grants_access)
            self.assertIn(sub, RoleSubscription.objects.granting_access_for(self.user))


class RoleSubscriptionGrantsAccessParityTests(TestCase):
    """grants_access (the model property) and granting_access_for() (the queryset filter)
    encode the same rule in two places and must agree, or a display path checking one and
    an access-control path checking the other would silently drift apart."""

    def setUp(self) -> None:
        super().setUp()
        self.role = baker.make(SubscriptionRole)

    def test_property_and_queryset_agree_across_status_threshold_and_banked_combinations(self) -> None:
        now = timezone.now()
        banked_offsets = [None, timedelta(days=1), timedelta(days=-1)]
        for status in BillingSubscriptionStatus.values:
            for threshold_met in (True, False):
                for offset in banked_offsets:
                    # A fresh user per combination: the uniqueness constraint only allows
                    # one non-canceled RoleSubscription per (user, role), and most of the
                    # statuses under test here are non-canceled.
                    user = baker.make(User)
                    sub = baker.make(
                        RoleSubscription,
                        user=user,
                        role=self.role,
                        status=status,
                        threshold_met=threshold_met,
                        usage_covered_until=(now + offset) if offset is not None else None,
                    )
                    in_queryset = RoleSubscription.objects.granting_access_for(user).filter(pk=sub.pk).exists()
                    self.assertEqual(
                        sub.grants_access,
                        in_queryset,
                        f"status={status!r} threshold_met={threshold_met!r} offset={offset!r}: "
                        f"grants_access={sub.grants_access!r} but queryset membership={in_queryset!r}",
                    )
