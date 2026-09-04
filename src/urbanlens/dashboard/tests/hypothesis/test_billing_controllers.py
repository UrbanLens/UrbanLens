"""Tests for controllers.billing - the user-facing Settings > Membership section.

Every Stripe SDK call is made through services.billing.stripe_client, which is mocked
here rather than the underlying stripe module - these tests verify the controller calls
the service layer with the right arguments, not Stripe SDK wiring (that's
test_billing_stripe_client.py's job).
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingCustomer, BillingSubscriptionStatus, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole


class BillingSettingsSectionViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)

    def test_requires_login(self) -> None:
        response = self.client.get(reverse("settings.billing"))
        self.assertEqual(response.status_code, 302)

    def test_lists_purchasable_roles(self) -> None:
        self.client.force_login(self.user)
        baker.make(SubscriptionRole, name="VIP", monthly_price_cents=500)
        baker.make(SubscriptionRole, name="Not for sale", monthly_price_cents=None, pay_what_you_want=False)

        response = self.client.get(reverse("settings.billing"))

        role_names = {row["role"].name for row in response.context["role_rows"]}
        self.assertIn("VIP", role_names)
        self.assertNotIn("Not for sale", role_names)

    def test_lists_pay_what_you_want_only_role(self) -> None:
        """The listing query ORs monthly_price_cents__isnull=False with pay_what_you_want=True
        - test_lists_purchasable_roles above only ever exercises the fixed-price side, so a
        mutation collapsing that OR into just the fixed-price filter would still pass it."""
        self.client.force_login(self.user)
        baker.make(SubscriptionRole, name="Patron", monthly_price_cents=None, pay_what_you_want=True)

        response = self.client.get(reverse("settings.billing"))

        role_names = {row["role"].name for row in response.context["role_rows"]}
        self.assertIn("Patron", role_names)

    def test_canceled_subscription_with_unexpired_banked_access_is_visible(self) -> None:
        self.client.force_login(self.user)
        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        subscription = baker.make(
            RoleSubscription,
            user=self.user,
            role=role,
            status=BillingSubscriptionStatus.CANCELED,
            usage_covered_until=timezone.now() + timedelta(days=5),
        )

        response = self.client.get(reverse("settings.billing"))

        self.assertIn(subscription, response.context["subscriptions"])
        self.assertNotContains(response, reverse("settings.billing.cancel", args=[subscription.pk]))

    def test_canceled_subscription_with_expired_banked_access_stays_excluded(self) -> None:
        self.client.force_login(self.user)
        role = baker.make(SubscriptionRole, pay_what_you_want=True, pwyw_minimum_cents=500)
        subscription = baker.make(
            RoleSubscription,
            user=self.user,
            role=role,
            status=BillingSubscriptionStatus.CANCELED,
            usage_covered_until=timezone.now() - timedelta(days=1),
        )

        response = self.client.get(reverse("settings.billing"))

        self.assertNotIn(subscription, response.context["subscriptions"])

    def test_canceled_subscription_with_no_banked_access_stays_excluded(self) -> None:
        self.client.force_login(self.user)
        subscription = baker.make(RoleSubscription, user=self.user, status=BillingSubscriptionStatus.CANCELED)

        response = self.client.get(reverse("settings.billing"))

        self.assertNotIn(subscription, response.context["subscriptions"])

    def test_active_subscription_is_visible(self) -> None:
        """visible_for's ~Q(status=CANCELED) clause is what makes a live subscription show up
        at all - every other test in this class exercises only CANCELED rows, so a mutation
        that dropped this clause (requiring banked access even for a currently-paying
        subscriber) would still pass them all."""
        self.client.force_login(self.user)
        subscription = baker.make(RoleSubscription, user=self.user, status=BillingSubscriptionStatus.ACTIVE)

        response = self.client.get(reverse("settings.billing"))

        self.assertIn(subscription, response.context["subscriptions"])


class BillingCheckoutViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_requires_login(self) -> None:
        self.client.logout()
        role = baker.make(SubscriptionRole, monthly_price_cents=500)
        response = self.client.post(reverse("settings.billing.checkout"), {"role_slug": role.slug})
        self.assertEqual(response.status_code, 302)

    def test_non_purchasable_role_redirects_without_calling_stripe(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=False)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            response = self.client.post(reverse("settings.billing.checkout"), {"role_slug": role.slug})

        self.assertEqual(response.status_code, 302)
        mock_create.assert_not_called()

    def test_fixed_price_role_uses_the_roles_price(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=500, pay_what_you_want=False)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            mock_create.return_value = mock.MagicMock(url="https://checkout.stripe.com/x")
            response = self.client.post(reverse("settings.billing.checkout"), {"role_slug": role.slug})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://checkout.stripe.com/x")
        _args, kwargs = mock_create.call_args
        self.assertEqual(kwargs["amount_cents"], 500)

    def test_pwyw_role_uses_the_submitted_amount(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=True)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            mock_create.return_value = mock.MagicMock(url="https://checkout.stripe.com/x")
            self.client.post(reverse("settings.billing.checkout"), {"role_slug": role.slug, "amount_dollars": "12.50"})

        _args, kwargs = mock_create.call_args
        self.assertEqual(kwargs["amount_cents"], 1250)

    def test_amount_below_stripe_minimum_redirects_without_calling_stripe(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=True)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            response = self.client.post(
                reverse("settings.billing.checkout"), {"role_slug": role.slug, "amount_dollars": "0.10"}
            )

        self.assertEqual(response.status_code, 302)
        mock_create.assert_not_called()

    def test_amount_at_stripe_minimum_calls_stripe(self) -> None:
        """Exact boundary: the check is `amount_cents < STRIPE_MINIMUM_CHARGE_CENTS`, so 50
        cents itself must succeed - a `<` -> `<=` mutation would wrongly reject it, and the
        far-below-minimum test above (10 cents) wouldn't notice."""
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=True)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            mock_create.return_value = mock.MagicMock(url="https://checkout.stripe.com/x")
            response = self.client.post(
                reverse("settings.billing.checkout"), {"role_slug": role.slug, "amount_dollars": "0.50"}
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://checkout.stripe.com/x")
        _args, kwargs = mock_create.call_args
        self.assertEqual(kwargs["amount_cents"], 50)

    def test_amount_one_cent_below_minimum_redirects_without_calling_stripe(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=None, pay_what_you_want=True)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            response = self.client.post(
                reverse("settings.billing.checkout"), {"role_slug": role.slug, "amount_dollars": "0.49"}
            )

        self.assertEqual(response.status_code, 302)
        mock_create.assert_not_called()

    def test_a_session_without_a_url_redirects_to_settings_instead_of_crashing(self) -> None:
        role = baker.make(SubscriptionRole, monthly_price_cents=500)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.create_checkout_session") as mock_create:
            mock_create.return_value = mock.MagicMock(url=None)
            response = self.client.post(reverse("settings.billing.checkout"), {"role_slug": role.slug})

        self.assertEqual(response.status_code, 302)
        self.assertIn("settings", response["Location"])


class BillingPortalViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_without_a_billing_customer_redirects_without_calling_stripe(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.controllers.billing.stripe_client.create_billing_portal_session"
        ) as mock_create:
            response = self.client.post(reverse("settings.billing.portal"))

        self.assertEqual(response.status_code, 302)
        mock_create.assert_not_called()

    def test_with_a_billing_customer_redirects_to_the_portal_url(self) -> None:
        baker.make(BillingCustomer, user=self.user)
        with mock.patch(
            "urbanlens.dashboard.controllers.billing.stripe_client.create_billing_portal_session"
        ) as mock_create:
            mock_create.return_value = "https://billing.stripe.com/x"
            response = self.client.post(reverse("settings.billing.portal"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://billing.stripe.com/x")
        _args, kwargs = mock_create.call_args
        self.assertEqual(kwargs["user"], self.user)
        self.assertIn("membership-settings-section", kwargs["return_url"])


class BillingPledgeUpdateViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.role = baker.make(SubscriptionRole, pay_what_you_want=True)
        self.subscription = baker.make(RoleSubscription, user=self.user, role=self.role, pledged_amount_cents=500)

    def test_invalid_amount_returns_400_and_does_not_call_stripe(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.update_pledge") as mock_update:
            response = self.client.post(
                reverse("settings.billing.pledge", args=[self.subscription.pk]), {"amount_dollars": "not-a-number"}
            )

        self.assertEqual(response.status_code, 400)
        mock_update.assert_not_called()

    def test_valid_amount_calls_update_pledge(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.update_pledge") as mock_update:
            response = self.client.post(
                reverse("settings.billing.pledge", args=[self.subscription.pk]), {"amount_dollars": "8.00"}
            )

        self.assertEqual(response.status_code, 200)
        mock_update.assert_called_once_with(self.subscription, 800)

    def test_amount_below_stripe_minimum_returns_400_and_does_not_call_stripe(self) -> None:
        """Distinct branch from test_invalid_amount_... above: a parseable amount that's
        merely too small, not an unparseable string - both take the `amount_cents is None or
        amount_cents < MINIMUM` guard, but only this exercises the `< MINIMUM` half."""
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.update_pledge") as mock_update:
            response = self.client.post(
                reverse("settings.billing.pledge", args=[self.subscription.pk]), {"amount_dollars": "0.49"}
            )

        self.assertEqual(response.status_code, 400)
        mock_update.assert_not_called()

    def test_amount_at_stripe_minimum_calls_update_pledge(self) -> None:
        """Exact boundary: 50 cents itself must succeed - a `<` -> `<=` mutation would
        wrongly reject it."""
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.update_pledge") as mock_update:
            response = self.client.post(
                reverse("settings.billing.pledge", args=[self.subscription.pk]), {"amount_dollars": "0.50"}
            )

        self.assertEqual(response.status_code, 200)
        mock_update.assert_called_once_with(self.subscription, 50)

    def test_cannot_update_another_users_subscription(self) -> None:
        other_subscription = baker.make(RoleSubscription, role=self.role)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.update_pledge") as mock_update:
            response = self.client.post(
                reverse("settings.billing.pledge", args=[other_subscription.pk]), {"amount_dollars": "8.00"}
            )

        self.assertEqual(response.status_code, 404)
        mock_update.assert_not_called()


class BillingCancelViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.subscription = baker.make(RoleSubscription, user=self.user)

    def test_cancels_via_the_stripe_client(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.cancel_at_period_end") as mock_cancel:
            response = self.client.post(reverse("settings.billing.cancel", args=[self.subscription.pk]))

        self.assertEqual(response.status_code, 200)
        mock_cancel.assert_called_once_with(self.subscription)

    def test_cannot_cancel_another_users_subscription(self) -> None:
        other_subscription = baker.make(RoleSubscription)
        with mock.patch("urbanlens.dashboard.controllers.billing.stripe_client.cancel_at_period_end") as mock_cancel:
            response = self.client.post(reverse("settings.billing.cancel", args=[other_subscription.pk]))

        self.assertEqual(response.status_code, 404)
        mock_cancel.assert_not_called()


class BillingCheckoutReturnViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_success_view_redirects_to_settings(self) -> None:
        response = self.client.get(reverse("settings.billing.checkout_success"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("settings", response["Location"])

    def test_cancel_view_redirects_to_settings(self) -> None:
        response = self.client.get(reverse("settings.billing.checkout_cancel"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("settings", response["Location"])
