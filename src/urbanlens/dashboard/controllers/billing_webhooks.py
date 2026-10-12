"""Stripe webhook receiver."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
import stripe

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(View):
    """
    Receive and process Stripe webhook events.

    POST /billing/webhooks/stripe/

    The first (and, deliberately, only) CSRF-exempt endpoint in this codebase: Stripe posts
    server-to-server with no Django session/CSRF token, so the request is authenticated instead by
    verifying the ``Stripe-Signature`` header against the raw body using the configured webhook secret.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.stripe_webhook_secret:
            # No secret can ever verify, so reject permanently like a bad signature.
            logger.error("Stripe webhook received but UL_STRIPE_WEBHOOK_SECRET is not set.")
            return HttpResponse(status=400)

        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            stripe_event = stripe.Webhook.construct_event(request.body, sig_header, app_settings.stripe_webhook_secret)
        except (ValueError, stripe.SignatureVerificationError):
            logger.warning("Rejected Stripe webhook with invalid payload/signature.")
            return HttpResponse(status=400)

        # stripe.Event lacks .get(); convert once for dict access and JSONField storage.
        event = stripe_event.to_dict()

        from urbanlens.dashboard.models.billing import StripeWebhookEvent
        from urbanlens.dashboard.services.billing import stripe_client, webhooks as billing_webhooks

        # Handlers call the Stripe API; an unset key is a deployment fault, so fail loudly and let Stripe redeliver.
        stripe_client.configure()

        StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event["id"],
            defaults={"event_type": event["type"], "payload": event},
        )

        with transaction.atomic():
            # Row lock prevents concurrent duplicate deliveries from double-processing.
            webhook_event = StripeWebhookEvent.objects.select_for_update().get(stripe_event_id=event["id"])
            if webhook_event.processed_at is not None:
                return HttpResponse(status=200)

            billing_webhooks.handle_event(event)

            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["processed_at", "updated"])
        return HttpResponse(status=200)
