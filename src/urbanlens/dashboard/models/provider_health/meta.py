"""Choices for ``ProviderHealth``."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class ProviderState(abstract.TextChoices):
    """Where a provider stands, as ``services.core.provider_health`` last judged it.

    Attributes:
        HEALTHY: Answering about as often as it normally does.
        DEGRADED: Answering, but well below its own 7-day normal, or answering empty far more often. Alerted on;
            nothing is paused.
        BACKED_OFF: Refusing this deployment, or answering almost nothing. Background calls are paused and live calls
            trickle until ``backed_off_until``.
        PROBING: The backoff ran out, and a few calls are let through to see whether it recovered.
    """

    HEALTHY = "healthy", "Healthy"
    DEGRADED = "degraded", "Degraded"
    BACKED_OFF = "backed_off", "Backed off"
    PROBING = "probing", "Probing"


class BackoffCause(abstract.TextChoices):
    """Why a provider left ``healthy``.

    Attributes:
        NONE: It has not.
        REFUSED: It said no - 401, 403 or 429.
        FAILING: 5xx, timeouts or failed connections, with hardly anything answered.
        BELOW_BASELINE: Answering much less, or much emptier, than its own normal.
    """

    NONE = "", "-"
    REFUSED = "refused", "Refusing this deployment"
    FAILING = "failing", "Failing"
    BELOW_BASELINE = "below_baseline", "Below its normal"
