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
    """Receive and process Stripe webhook events.

    POST /billing/webhooks/stripe/

    The first (and, deliberately, only) CSRF-exempt endpoint in this codebase: Stripe posts
    server-to-server with no Django session/CSRF token, so the request is authenticated instead by
    verifying the ``Stripe-Signature`` header against the raw body using the configured webhook secret.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.stripe_webhook_secret:
            # There is no signature this deployment could ever accept while unconfigured, so it's a permanent
            # refusal like a bad signature, not a transient one - same status as the construct_event failure
            # below.
            logger.error("Stripe webhook received but UL_STRIPE_WEBHOOK_SECRET is not set.")
            return HttpResponse(status=400)

        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            stripe_event = stripe.Webhook.construct_event(request.body, sig_header, app_settings.stripe_webhook_secret)
        except (ValueError, stripe.SignatureVerificationError):
            logger.warning("Rejected Stripe webhook with invalid payload/signature.")
            return HttpResponse(status=400)

        # stripe.Event doesn't implement .get() - convert to a plain dict once at the boundary so every
        # downstream handler (and JSONField storage below) can use ordinary dict access instead of the SDK's
        # attribute-only interface.
        event = stripe_event.to_dict()

        from urbanlens.dashboard.models.billing import StripeWebhookEvent
        from urbanlens.dashboard.services.billing import webhooks as billing_webhooks

        # Handling and marking-as-handled have to commit together.
        StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event["id"],
            defaults={"event_type": event["type"], "payload": event},
        )

        with transaction.atomic():
            # Re-read under a row lock: two deliveries of one event arriving at once would otherwise both read
            # processed_at as null and both run the side effects.
            webhook_event = StripeWebhookEvent.objects.select_for_update().get(stripe_event_id=event["id"])
            if webhook_event.processed_at is not None:
                return HttpResponse(status=200)

            billing_webhooks.handle_event(event)

            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["processed_at", "updated"])
        return HttpResponse(status=200)
