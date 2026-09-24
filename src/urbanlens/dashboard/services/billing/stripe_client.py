"""Thin wrapper around the Stripe SDK for checkout, pledge updates, and the billing portal."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
import stripe

from urbanlens.dashboard.models.billing import BillingCustomer, RoleSubscription

if TYPE_CHECKING:
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

logger = logging.getLogger(__name__)

_PRICE_CURRENCY = "usd"


class AlreadySubscribedError(Exception):
    """The user already holds a live paid subscription to this role."""


def is_configured() -> bool:
    """Whether a Stripe secret key is configured for this environment."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return bool(app_settings.stripe_secret_key)


def configure() -> None:
    """Point the Stripe SDK at the configured secret key, or raise if unset.

    Every entry point that reaches the SDK calls this first: the key is process-global, so a worker that has not run
    it has none.

    Raises:
        ImproperlyConfigured: When ``stripe_secret_key`` isn't set.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not app_settings.stripe_secret_key:
        raise ImproperlyConfigured("UL_STRIPE_SECRET_KEY is not set - paid subscriptions are unavailable.")
    stripe.api_key = app_settings.stripe_secret_key


def idempotency_key(kind: str, *parts: object) -> str:
    """A Stripe idempotency key for one local action.

    Namespaced by ``SITE_URL`` because deployments can share a Stripe account while numbering their rows alike. Callers
    include the request's parameters in *parts*: Stripe refuses a reused key whose parameters differ.

    Args:
        kind: What is being created or changed, e.g. ``"customer"``.
        *parts: The local identity and parameters of the request.

    Returns:
        A key that is stable for the same deployment, identity and parameters.
    """
    material = json.dumps([settings.SITE_URL, kind, *parts], sort_keys=True, default=str)
    return f"urbanlens-{kind}-{hashlib.sha256(material.encode()).hexdigest()[:48]}"


def lock_billing_owner(user: User) -> None:
    """Take the row lock that serialises one user's billing writes: customer creation and new subscriptions.

    Call inside ``transaction.atomic``.

    Args:
        user: The billing owner.
    """
    User.objects.select_for_update().filter(pk=user.pk).values_list("pk", flat=True).first()


def ensure_customer(user: User) -> BillingCustomer:
    """Return the user's BillingCustomer, creating the Stripe Customer + row if needed.

    Args:
        user: The user to look up or create a Stripe Customer for.

    Returns:
        The (possibly newly created) BillingCustomer row.
    """
    configure()
    with transaction.atomic():
        lock_billing_owner(user)
        existing = BillingCustomer.objects.filter(user=user).first()
        if existing is not None:
            return existing

        create_kwargs: stripe.params.CustomerCreateParams = {
            "name": user.get_full_name() or user.username,
            "metadata": {"user_id": str(user.pk)},
        }
        if user.email:
            create_kwargs["email"] = user.email
        stripe_customer = stripe.Customer.create(**create_kwargs, idempotency_key=idempotency_key("customer", user.pk, create_kwargs))
        return BillingCustomer.objects.create(user=user, stripe_customer_id=stripe_customer.id)


def ensure_product(role: SubscriptionRole) -> str:
    """Return the role's Stripe Product id, creating it (and persisting it) if needed.

    Args:
        role: The role to look up or create a Stripe Product for.

    Returns:
        The Stripe Product id.
    """
    configure()
    if role.stripe_product_id:
        return role.stripe_product_id

    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

    with transaction.atomic():
        locked = SubscriptionRole.objects.select_for_update().get(pk=role.pk)
        if not locked.stripe_product_id:
            create_kwargs: stripe.params.ProductCreateParams = {
                "name": f"UrbanLens - {locked.name}",
                "metadata": {"role_id": str(locked.pk), "role_slug": locked.slug},
            }
            product = stripe.Product.create(**create_kwargs, idempotency_key=idempotency_key("product", locked.pk, create_kwargs))
            locked.stripe_product_id = product.id
            SubscriptionRole.objects.filter(pk=locked.pk).update(stripe_product_id=product.id)
    role.stripe_product_id = locked.stripe_product_id
    return locked.stripe_product_id


def create_checkout_session(user: User, role: SubscriptionRole, amount_cents: int, success_url: str, cancel_url: str) -> stripe.checkout.Session:
    """Create a Checkout Session for subscribing *user* to *role* at *amount_cents*/month.

    Args:
        user: The subscribing user.
        role: The role being subscribed to.
        amount_cents: The monthly amount to charge, in cents (the role's fixed price, or a user-chosen pay-what-you-want amount).
        success_url: Where Stripe redirects on successful checkout.
        cancel_url: Where Stripe redirects if the user abandons checkout.

    Returns:
        The created Checkout Session (redirect the browser to ``.url``).

    Raises:
        AlreadySubscribedError: When the user already holds a live subscription to *role*; a second one could not be
            recorded, and Stripe would bill both."""
    configure()
    if RoleSubscription.objects.not_terminal().filter(user=user, role=role).exists():
        raise AlreadySubscribedError(role.slug)
    customer = ensure_customer(user)
    product_id = ensure_product(role)
    metadata = {"user_id": str(user.pk), "role_id": str(role.pk)}
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.stripe_customer_id,
        client_reference_id=str(user.pk),
        # Set on both the Session (read by checkout.session.completed) and the resulting
        # Subscription (read by every other webhook, which never sees the Session) - otherwise a
        # webhook arriving without the Session in hand has no way to resolve which role a bare
        # Stripe subscription id belongs to.
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
    Takes effect at the next billing cycle (``proration_behavior="none"``) rather than immediately reprorating/charging - matches the site's own "the following month" framing for when a changed pledge starts counting toward a dynamic threshold.

    Args:
        role_subscription: The subscription to update.
        new_amount_cents: The new monthly pledge, in cents.

    Returns:
        The updated Stripe Subscription."""
    configure()
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
    configure()
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
        BillingCustomer.DoesNotExist: When the user has no Stripe customer yet."""
    configure()
    customer = BillingCustomer.objects.get(user=user)
    session = stripe.billing_portal.Session.create(customer=customer.stripe_customer_id, return_url=return_url)
    return session.url
