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

    The first (and, deliberately, only) CSRF-exempt endpoint in this codebase: Stripe
    posts server-to-server with no Django session/CSRF token, so the request is
    authenticated instead by verifying the ``Stripe-Signature`` header against the raw
    body using the configured webhook secret. Processing runs synchronously in-request
    (store the event, handle it, respond) rather than via Celery, matching how this
    codebase's OAuth callback views (Flickr/Google Photos/Immich) already do their
    credential/state writes inline - Celery here is reserved for actual bulk/long-running
    work, not a handful of row upserts, and Stripe's timeout tolerance is generous.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.stripe_webhook_secret:
            logger.error("Stripe webhook received but UL_STRIPE_WEBHOOK_SECRET is not set.")
            return HttpResponse(status=503)

        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            stripe_event = stripe.Webhook.construct_event(request.body, sig_header, app_settings.stripe_webhook_secret)
        except (ValueError, stripe.SignatureVerificationError):
            logger.warning("Rejected Stripe webhook with invalid payload/signature.")
            return HttpResponse(status=400)

        # stripe.Event doesn't implement .get() - convert to a plain dict once at the
        # boundary so every downstream handler (and JSONField storage below) can use
        # ordinary dict access instead of the SDK's attribute-only interface.
        event = stripe_event.to_dict()

        from urbanlens.dashboard.models.billing import StripeWebhookEvent
        from urbanlens.dashboard.services.billing import webhooks as billing_webhooks

        # Handling and marking-as-handled have to commit together. ``invoice.payment_succeeded``
        # credits the payment by *incrementing* total_paid_cents (which drives how long
        # pay-what-you-want access stays granted), so a delivery that applies the credit but
        # fails before recording processed_at gets redelivered - Stripe retries on any
        # non-2xx *or timeout* - and credits it a second time.
        # Recorded in its own transaction, before the one below, so a delivery whose handler
        # blows up still leaves its raw payload behind to debug from.
        StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event["id"],
            defaults={"event_type": event["type"], "payload": event},
        )

        with transaction.atomic():
            # Re-read under a row lock: two deliveries of one event arriving at once would
            # otherwise both read processed_at as null and both run the side effects. The lock
            # is per-event-id, so it only ever blocks a duplicate of this same event, and it is
            # held across handle_event's Stripe API call - a round-trip, not a long job.
            webhook_event = StripeWebhookEvent.objects.select_for_update().get(stripe_event_id=event["id"])
            if webhook_event.processed_at is not None:
                return HttpResponse(status=200)

            billing_webhooks.handle_event(event)

            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["processed_at", "updated"])
        return HttpResponse(status=200)
