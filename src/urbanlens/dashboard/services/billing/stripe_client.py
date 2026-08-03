"""Thin wrapper around the Stripe SDK for checkout, pledge updates, and the billing portal.

Every checkout/pledge-update uses ``price_data`` inline (never a pre-created Stripe
``Price``) against a lazily-created Stripe ``Product`` per role - this lets an admin
freely edit ``SubscriptionRole.monthly_price_cents`` without needing to archive/recreate
Stripe ``Price`` objects, and lets a pay-what-you-want pledge be any amount.

Uses the module-level ``stripe`` API (``stripe.checkout.Session.create`` etc.) rather
than a ``StripeClient`` instance, so tests can mock calls directly via
``mock.patch("stripe.X.Y")``, matching how this codebase mocks other external SDKs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
import stripe

from urbanlens.dashboard.models.billing import BillingCustomer

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.billing import RoleSubscription
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

logger = logging.getLogger(__name__)

_PRICE_CURRENCY = "usd"


def is_configured() -> bool:
    """Whether a Stripe secret key is configured for this environment."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return bool(app_settings.stripe_secret_key)


def _ensure_configured() -> None:
    """Point the Stripe SDK at the configured secret key, or raise if unset.

    Raises:
        ImproperlyConfigured: When ``stripe_secret_key`` isn't set.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not app_settings.stripe_secret_key:
        raise ImproperlyConfigured("UL_STRIPE_SECRET_KEY is not set - paid subscriptions are unavailable.")
    stripe.api_key = app_settings.stripe_secret_key


def ensure_customer(user: User) -> BillingCustomer:
    """Return the user's BillingCustomer, creating the Stripe Customer + row if needed.

    Args:
        user: The user to look up or create a Stripe Customer for.

    Returns:
        The (possibly newly created) BillingCustomer row.
    """
    _ensure_configured()
    existing = BillingCustomer.objects.filter(user=user).first()
    if existing is not None:
        return existing

    create_kwargs: stripe.params.CustomerCreateParams = {
        "name": user.get_full_name() or user.username,
        "metadata": {"user_id": str(user.pk)},
    }
    if user.email:
        create_kwargs["email"] = user.email
    stripe_customer = stripe.Customer.create(**create_kwargs)
    customer, _created = BillingCustomer.objects.get_or_create(user=user, defaults={"stripe_customer_id": stripe_customer.id})
    return customer


def ensure_product(role: SubscriptionRole) -> str:
    """Return the role's Stripe Product id, creating it (and persisting it) if needed.

    Args:
        role: The role to look up or create a Stripe Product for.

    Returns:
        The Stripe Product id.
    """
    _ensure_configured()
    if role.stripe_product_id:
        return role.stripe_product_id

    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

    product = stripe.Product.create(
        name=f"UrbanLens - {role.name}",
        metadata={"role_id": str(role.pk), "role_slug": role.slug},
    )
    SubscriptionRole.objects.filter(pk=role.pk).update(stripe_product_id=product.id)
    role.stripe_product_id = product.id
    return product.id


def create_checkout_session(user: User, role: SubscriptionRole, amount_cents: int, success_url: str, cancel_url: str) -> stripe.checkout.Session:
    """Create a Checkout Session for subscribing *user* to *role* at *amount_cents*/month.

    Args:
        user: The subscribing user.
        role: The role being subscribed to.
        amount_cents: The monthly amount to charge, in cents (the role's fixed price, or
            a user-chosen pay-what-you-want amount).
        success_url: Where Stripe redirects on successful checkout.
        cancel_url: Where Stripe redirects if the user abandons checkout.

    Returns:
        The created Checkout Session (redirect the browser to ``.url``).
    """
    _ensure_configured()
    customer = ensure_customer(user)
    product_id = ensure_product(role)
    metadata = {"user_id": str(user.pk), "role_id": str(role.pk)}
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.stripe_customer_id,
        client_reference_id=str(user.pk),
        # Set on both the Session (read by checkout.session.completed) and the resulting
        # Subscription (read by every other webhook, which never sees the Session) -
        # otherwise a webhook arriving without the Session in hand has no way to
        # resolve which role a bare Stripe subscription id belongs to.
        metadata=metadata,
        line_items=[
            {
                "price_data": {
                    "currency": _PRICE_CURRENCY,
                    "product": product_id,
                    "unit_amount": amount_cents,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }
        ],
        subscription_data={"metadata": metadata},
        success_url=success_url,
        cancel_url=cancel_url,
    )


def update_pledge(role_subscription: RoleSubscription, new_amount_cents: int) -> stripe.Subscription:
    """Change an existing pay-what-you-want subscription's pledge amount.

    Takes effect at the next billing cycle (``proration_behavior="none"``) rather than
    immediately reprorating/charging - matches the site's own "the following month"
    framing for when a changed pledge starts counting toward a dynamic threshold.

    Args:
        role_subscription: The subscription to update.
        new_amount_cents: The new monthly pledge, in cents.

    Returns:
        The updated Stripe Subscription.
    """
    _ensure_configured()
    stripe_subscription = stripe.Subscription.retrieve(role_subscription.stripe_subscription_id).to_dict()
    item_id = stripe_subscription["items"]["data"][0]["id"]
    product_id = ensure_product(role_subscription.role)
    return stripe.Subscription.modify(
        role_subscription.stripe_subscription_id,
        items=[
            {
                "id": item_id,
                "price_data": {
                    "currency": _PRICE_CURRENCY,
                    "product": product_id,
                    "unit_amount": new_amount_cents,
                    "recurring": {"interval": "month"},
                },
            }
        ],
        proration_behavior="none",
    )


def cancel_at_period_end(role_subscription: RoleSubscription) -> stripe.Subscription:
    """Cancel a subscription at the end of its current billing period.

    Args:
        role_subscription: The subscription to cancel.

    Returns:
        The updated Stripe Subscription.
    """
    _ensure_configured()
    result = stripe.Subscription.modify(role_subscription.stripe_subscription_id, cancel_at_period_end=True)
    type(role_subscription).objects.filter(pk=role_subscription.pk).update(cancel_at_period_end=True)
    return result


def create_billing_portal_session(user: User, return_url: str) -> str:
    """Create a Stripe billing portal session for *user* and return its URL.

    Args:
        user: The user managing their billing (must already have a BillingCustomer).
        return_url: Where Stripe redirects once the user leaves the portal.

    Returns:
        The portal session URL to redirect the browser to.

    Raises:
        BillingCustomer.DoesNotExist: When the user has no Stripe customer yet.
    """
    _ensure_configured()
    customer = BillingCustomer.objects.get(user=user)
    session = stripe.billing_portal.Session.create(customer=customer.stripe_customer_id, return_url=return_url)
    return session.url
