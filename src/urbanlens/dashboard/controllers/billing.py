"""User-facing paid subscription controller: browse/subscribe/manage from Settings.

Checkout and the billing portal are genuine cross-site redirects to Stripe-hosted
pages, so those views use plain (non-HTMX) form POSTs that 302 the whole browser -
HTMX would try to swap the redirect target's HTML into the page instead of navigating
to it. Everything that stays on-site (viewing the section, updating a pledge,
cancelling) uses the same lazy-HTMX-subsection pattern as Immich/Flickr/Google Photos.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.billing import BillingCustomer, RoleSubscription
from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole
from urbanlens.dashboard.services.billing import pricing, stripe_client

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_SECTION_PARTIAL = "dashboard/partials/settings/_billing_section.html"


def _with_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


def _parse_amount_cents(raw: str | None) -> int | None:
    """Parse a user-entered dollar amount into cents, or None if unparseable/non-positive."""
    if not raw:
        return None
    try:
        dollars = Decimal(raw)
    except InvalidOperation:
        return None
    if dollars <= 0:
        return None
    return pricing.dollars_to_cents(dollars)


class BillingSettingsSectionView(LoginRequiredMixin, View):
    """GET /settings/billing/ - HTMX subsection: purchasable roles + current paid subscriptions."""

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, _SECTION_PARTIAL, _section_context(request))


def _section_context(request: HttpRequest) -> dict:
    purchasable_roles = SubscriptionRole.objects.filter(monthly_price_cents__isnull=False) | SubscriptionRole.objects.filter(pay_what_you_want=True)
    role_rows = []
    for role in purchasable_roles.distinct().order_by("name"):
        threshold_cents = pricing.role_pwyw_threshold_cents(role)
        role_rows.append(
            {
                "role": role,
                "pwyw_threshold_dollars": pricing.cents_to_dollars(threshold_cents) if threshold_cents else None,
            }
        )
    subscriptions = RoleSubscription.objects.visible_for(request.user).select_related("role").order_by("-created")
    return {
        "stripe_configured": stripe_client.is_configured(),
        "role_rows": role_rows,
        "subscriptions": subscriptions,
        "has_billing_customer": BillingCustomer.objects.filter(user=request.user).exists(),
        "minimum_pledge_dollars": pricing.cents_to_dollars(pricing.STRIPE_MINIMUM_CHARGE_CENTS),
    }


class BillingCheckoutView(LoginRequiredMixin, View):
    """POST /settings/billing/checkout/ - create a Checkout Session and redirect to Stripe."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return HttpResponseForbidden()

        role = get_object_or_404(SubscriptionRole, slug=request.POST.get("role_slug", ""))
        if not role.is_purchasable:
            return redirect(f"{reverse('settings.view')}#membership-settings-section")

        amount_cents = _parse_amount_cents(request.POST.get("amount_dollars")) if role.pay_what_you_want else role.monthly_price_cents
        if amount_cents is None or amount_cents < pricing.STRIPE_MINIMUM_CHARGE_CENTS:
            return redirect(f"{reverse('settings.view')}#membership-settings-section")

        session = stripe_client.create_checkout_session(
            user=request.user,
            role=role,
            amount_cents=amount_cents,
            success_url=request.build_absolute_uri(reverse("settings.billing.checkout_success")),
            cancel_url=request.build_absolute_uri(reverse("settings.billing.checkout_cancel")),
        )
        if not session.url:
            logger.error("Stripe Checkout Session %s was created without a redirect url.", session.id)
            return redirect(f"{reverse('settings.view')}#membership-settings-section")
        return HttpResponseRedirect(session.url)


class BillingPortalView(LoginRequiredMixin, View):
    """POST /settings/billing/portal/ - create a Stripe billing portal session and redirect."""

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return HttpResponseForbidden()

        if not BillingCustomer.objects.filter(user=request.user).exists():
            return redirect(f"{reverse('settings.view')}#membership-settings-section")
        url = stripe_client.create_billing_portal_session(
            user=request.user,
            return_url=request.build_absolute_uri(f"{reverse('settings.view')}#membership-settings-section"),
        )
        return HttpResponseRedirect(url)


class BillingPledgeUpdateView(LoginRequiredMixin, View):
    """POST /settings/billing/<id>/pledge/ - change an existing pay-what-you-want pledge."""

    def post(self, request: HttpRequest, subscription_id: int) -> HttpResponse:
        subscription = get_object_or_404(RoleSubscription, pk=subscription_id, user=request.user)
        amount_cents = _parse_amount_cents(request.POST.get("amount_dollars"))
        if amount_cents is None or amount_cents < pricing.STRIPE_MINIMUM_CHARGE_CENTS:
            response = render(request, _SECTION_PARTIAL, _section_context(request), status=400)
            return _with_toast(response, "Enter a valid pledge amount.", level="error")

        stripe_client.update_pledge(subscription, amount_cents)
        response = render(request, _SECTION_PARTIAL, _section_context(request))
        return _with_toast(response, "Pledge updated - takes effect next billing cycle.")


class BillingCancelView(LoginRequiredMixin, View):
    """POST /settings/billing/<id>/cancel/ - cancel a paid subscription at period end."""

    def post(self, request: HttpRequest, subscription_id: int) -> HttpResponse:
        subscription = get_object_or_404(RoleSubscription, pk=subscription_id, user=request.user)
        stripe_client.cancel_at_period_end(subscription)
        response = render(request, _SECTION_PARTIAL, _section_context(request))
        return _with_toast(response, f"{subscription.role.name} will cancel at the end of the current billing period.")


class BillingCheckoutSuccessView(LoginRequiredMixin, View):
    """GET /settings/billing/success/ - Stripe Checkout success redirect landing page.

    Real state comes from the webhook, not this redirect - Stripe can complete
    checkout.session.completed processing slightly after the browser lands here.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        messages.success(request, "Thanks! Your subscription is being set up.")
        return redirect(f"{reverse('settings.view')}#membership-settings-section")


class BillingCheckoutCancelView(LoginRequiredMixin, View):
    """GET /settings/billing/cancel-checkout/ - Stripe Checkout abandoned-checkout redirect."""

    def get(self, request: HttpRequest) -> HttpResponse:
        return redirect(f"{reverse('settings.view')}#membership-settings-section")
