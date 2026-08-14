from django.db.models import TextChoices


class BillingSubscriptionStatus(TextChoices):
    """Mirrors Stripe's own ``Subscription.status`` values."""

    INCOMPLETE = "incomplete", "Incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired", "Incomplete (expired)"
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past due"
    CANCELED = "canceled", "Canceled"
    UNPAID = "unpaid", "Unpaid"
    TRIALING = "trialing", "Trialing"
    PAUSED = "paused", "Paused"
