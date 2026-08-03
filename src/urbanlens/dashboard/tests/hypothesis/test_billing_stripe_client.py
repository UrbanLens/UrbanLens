"""Tests for services.billing.stripe_client - all Stripe SDK calls mocked, no real network."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.billing import BillingCustomer, RoleSubscription
from urbanlens.dashboard.models.subscriptions import SubscriptionRole
from urbanlens.dashboard.services.billing import stripe_client
from urbanlens.UrbanLens.settings.app import settings as app_settings


def _configured():
    return mock.patch.object(app_settings, "stripe_secret_key", "sk_test_123")


class IsConfiguredTests(TestCase):
    def test_true_when_secret_key_set(self) -> None:
        with _configured():
            self.assertTrue(stripe_client.is_configured())

    def test_false_when_secret_key_unset(self) -> None:
        with mock.patch.object(app_settings, "stripe_secret_key", None):
            self.assertFalse(stripe_client.is_configured())


class EnsureConfiguredTests(TestCase):
    def test_raises_when_unconfigured(self) -> None:
        with mock.patch.object(app_settings, "stripe_secret_key", None), pytest.raises(ImproperlyConfigured):
            stripe_client.ensure_customer(baker.make(User))


class EnsureCustomerTests(TestCase):
    def test_creates_a_stripe_customer_and_billing_customer_row(self) -> None:
        user = baker.make(User, email="person@example.test", first_name="", last_name="", username="person")
        with _configured(), mock.patch("stripe.Customer.create") as mock_create:
            mock_create.return_value = mock.MagicMock(id="cus_123")
            customer = stripe_client.ensure_customer(user)

        self.assertEqual(customer.stripe_customer_id, "cus_123")
        self.assertTrue(BillingCustomer.objects.filter(user=user, stripe_customer_id="cus_123").exists())
        _args, kwargs = mock_create.call_args
        self.assertEqual(kwargs["email"], "person@example.test")
        self.assertEqual(kwargs["metadata"], {"user_id": str(user.pk)})

    def test_omits_email_kwarg_when_user_has_none(self) -> None:
        user = baker.make(User, email="", username="person")
        with _configured(), mock.patch("stripe.Customer.create") as mock_create:
            mock_create.return_value = mock.MagicMock(id="cus_123")
            stripe_client.ensure_customer(user)

        _args, kwargs = mock_create.call_args
        self.assertNotIn("email", kwargs)

    def test_returns_the_existing_row_without_calling_stripe_again(self) -> None:
        user = baker.make(User)
        baker.make(BillingCustomer, user=user, stripe_customer_id="cus_existing")
        with _configured(), mock.patch("stripe.Customer.create") as mock_create:
            customer = stripe_client.ensure_customer(user)

        self.assertEqual(customer.stripe_customer_id, "cus_existing")
        mock_create.assert_not_called()


class EnsureProductTests(TestCase):
    def test_creates_and_persists_a_stripe_product(self) -> None:
        role = baker.make(SubscriptionRole, stripe_product_id="")
        with _configured(), mock.patch("stripe.Product.create") as mock_create:
            mock_create.return_value = mock.MagicMock(id="prod_123")
            product_id = stripe_client.ensure_product(role)

        self.assertEqual(product_id, "prod_123")
        role.refresh_from_db()
        self.assertEqual(role.stripe_product_id, "prod_123")

    def test_reuses_an_existing_product_id(self) -> None:
        role = baker.make(SubscriptionRole, stripe_product_id="prod_existing")
        with _configured(), mock.patch("stripe.Product.create") as mock_create:
            product_id = stripe_client.ensure_product(role)

        self.assertEqual(product_id, "prod_existing")
        mock_create.assert_not_called()


class CreateCheckoutSessionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.role = baker.make(SubscriptionRole, stripe_product_id="prod_123")
        baker.make(BillingCustomer, user=self.user, stripe_customer_id="cus_123")

    def test_sends_price_data_for_the_requested_amount(self) -> None:
        with _configured(), mock.patch("stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = mock.MagicMock(url="https://checkout.stripe.com/session")
            session = stripe_client.create_checkout_session(
                user=self.user,
                role=self.role,
                amount_cents=750,
                success_url="https://example.test/success",
                cancel_url="https://example.test/cancel",
            )

        self.assertEqual(session.url, "https://checkout.stripe.com/session")
        _args, kwargs = mock_create.call_args
        self.assertEqual(kwargs["mode"], "subscription")
        self.assertEqual(kwargs["customer"], "cus_123")
        self.assertEqual(kwargs["client_reference_id"], str(self.user.pk))
        self.assertEqual(kwargs["metadata"], {"user_id": str(self.user.pk), "role_id": str(self.role.pk)})
        self.assertEqual(kwargs["subscription_data"], {"metadata": kwargs["metadata"]})
        line_item = kwargs["line_items"][0]
        self.assertEqual(line_item["quantity"], 1)
        self.assertEqual(
            line_item["price_data"],
            {"currency": "usd", "product": "prod_123", "unit_amount": 750, "recurring": {"interval": "month"}},
        )


class UpdatePledgeTests(TestCase):
    def test_modifies_the_subscription_item_with_new_price_data(self) -> None:
        role = baker.make(SubscriptionRole, stripe_product_id="prod_123")
        subscription = baker.make(RoleSubscription, role=role, stripe_subscription_id="sub_123")
        retrieved = mock.MagicMock()
        retrieved.to_dict.return_value = {"items": {"data": [{"id": "si_123"}]}}
        with _configured(), mock.patch("stripe.Subscription.retrieve", return_value=retrieved), mock.patch("stripe.Subscription.modify") as mock_modify:
            stripe_client.update_pledge(subscription, 1200)

        _args, kwargs = mock_modify.call_args
        self.assertEqual(kwargs["proration_behavior"], "none")
        item = kwargs["items"][0]
        self.assertEqual(item["id"], "si_123")
        self.assertEqual(item["price_data"]["unit_amount"], 1200)
        self.assertEqual(item["price_data"]["product"], "prod_123")


class CancelAtPeriodEndTests(TestCase):
    def test_marks_cancel_at_period_end_locally_and_in_stripe(self) -> None:
        subscription = baker.make(RoleSubscription, stripe_subscription_id="sub_123", cancel_at_period_end=False)
        with _configured(), mock.patch("stripe.Subscription.modify") as mock_modify:
            stripe_client.cancel_at_period_end(subscription)

        mock_modify.assert_called_once_with("sub_123", cancel_at_period_end=True)
        subscription.refresh_from_db()
        self.assertTrue(subscription.cancel_at_period_end)


class CreateBillingPortalSessionTests(TestCase):
    def test_returns_the_portal_session_url(self) -> None:
        user = baker.make(User)
        baker.make(BillingCustomer, user=user, stripe_customer_id="cus_123")
        with _configured(), mock.patch("stripe.billing_portal.Session.create") as mock_create:
            mock_create.return_value = mock.MagicMock(url="https://billing.stripe.com/session")
            url = stripe_client.create_billing_portal_session(user, "https://example.test/return")

        self.assertEqual(url, "https://billing.stripe.com/session")
        mock_create.assert_called_once_with(customer="cus_123", return_url="https://example.test/return")

    def test_raises_when_user_has_no_billing_customer(self) -> None:
        user = baker.make(User)
        with _configured(), pytest.raises(BillingCustomer.DoesNotExist):
            stripe_client.create_billing_portal_session(user, "https://example.test/return")
