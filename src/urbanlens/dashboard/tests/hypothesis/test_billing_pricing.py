"""Tests for services.billing.pricing - pure money/threshold helpers, no Stripe/network calls."""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import pricing


class DollarsCentsConversionTests(SimpleTestCase):
    def test_dollars_to_cents_rounds_half_up(self) -> None:
        self.assertEqual(pricing.dollars_to_cents(Decimal("4.995")), 500)

    def test_dollars_to_cents_rounds_half_up_not_half_even(self) -> None:
        # 498.5 sits on an *even* lower bound, so ROUND_HALF_EVEN (banker's rounding) would
        # drop to 498 here - unlike the 4.995 case above, where both modes agree on 500 and
        # so can't tell ROUND_HALF_UP apart from a switch to banker's rounding.
        self.assertEqual(pricing.dollars_to_cents(Decimal("4.985")), 499)

    def test_dollars_to_cents_accepts_a_string(self) -> None:
        self.assertEqual(pricing.dollars_to_cents("9.99"), 999)

    def test_cents_to_dollars(self) -> None:
        self.assertEqual(pricing.cents_to_dollars(500), Decimal("5.00"))


class RolePwywThresholdCentsTests(TestCase):
    def test_non_pwyw_role_has_no_threshold(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=False)
        self.assertIsNone(pricing.role_pwyw_threshold_cents(role))

    def test_plain_pwyw_with_no_minimum_has_zero_threshold(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=None
        )
        self.assertEqual(pricing.role_pwyw_threshold_cents(role), 0)

    def test_plain_pwyw_with_a_static_minimum(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=300
        )
        self.assertEqual(pricing.role_pwyw_threshold_cents(role), 300)

    def test_dynamic_threshold_uses_cost_per_user(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        with mock.patch("urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("4.00")):
            self.assertEqual(pricing.role_pwyw_threshold_cents(role), 400)

    def test_dynamic_threshold_is_zero_when_there_are_no_active_users(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        with mock.patch("urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=None):
            self.assertEqual(pricing.role_pwyw_threshold_cents(role), 0)

    def test_dynamic_threshold_forwards_as_of_to_cost_per_user(self) -> None:
        # services.billing.banking backfills historical months by passing a moving `as_of`
        # cursor here; a dropped/defaulted argument would silently re-evaluate against "now"
        # instead, which the other dynamic-threshold tests above (all called with as_of=None)
        # can't catch.
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        as_of = datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC)
        with mock.patch(
            "urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("1.00")
        ) as mock_cost_per_user:
            pricing.role_pwyw_threshold_cents(role, as_of=as_of)
        mock_cost_per_user.assert_called_once_with(as_of)


class PledgeMeetsThresholdTests(TestCase):
    def test_non_pwyw_role_always_meets_it(self) -> None:
        role = baker.make(SubscriptionRole, pay_what_you_want=False)
        self.assertTrue(pricing.pledge_meets_threshold(role, 0))

    def test_pledge_below_static_minimum_fails(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.assertFalse(pricing.pledge_meets_threshold(role, 499))

    def test_pledge_at_static_minimum_passes(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=False, pwyw_minimum_cents=500
        )
        self.assertTrue(pricing.pledge_meets_threshold(role, 500))

    def test_pledge_below_dynamic_threshold_fails(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        with mock.patch("urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("5.00")):
            self.assertFalse(pricing.pledge_meets_threshold(role, 400))

    def test_pledge_meeting_dynamic_threshold_passes(self) -> None:
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        with mock.patch("urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("5.00")):
            self.assertTrue(pricing.pledge_meets_threshold(role, 500))

    def test_dynamic_threshold_forwards_as_of(self) -> None:
        # Proves the `as_of` this function accepts actually reaches cost_per_user, rather
        # than being dropped on the way through role_pwyw_threshold_cents.
        role = baker.make(
            SubscriptionRole, pay_what_you_want=True, pwyw_dynamic_threshold=True, pwyw_minimum_cents=None
        )
        as_of = datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC)
        with mock.patch(
            "urbanlens.dashboard.services.admin.cost_tracking.cost_per_user", return_value=Decimal("5.00")
        ) as mock_cost_per_user:
            pricing.pledge_meets_threshold(role, 500, as_of=as_of)
        mock_cost_per_user.assert_called_once_with(as_of)
