"""ApiCallLog model - records every external API call for rate limiting and observability."""

from __future__ import annotations

from django.db.models import BooleanField, CharField, DecimalField, Index, IntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.api_call_log.queryset import ApiCallLogManager


class ApiCallLog(abstract.DashboardModel):
    """Log entry for one external API call.

    The ``created`` timestamp (from the base model) is the call time.
    Rows are trimmed daily by ``tasks.prune_api_call_logs``; its retention is
    set by the *costs page's* 12-month spend chart, not by the 30-day
    rate-limit windows - see that task before shortening it.
    """

    service = CharField(
        max_length=50,
        db_index=True,
        help_text="Service identifier matching ApiRateLimit.service.",
    )
    endpoint = TextField(
        blank=True,
        help_text="URL or endpoint path called.",
    )
    success = BooleanField(
        default=True,
        help_text="False if the call raised an exception or returned a non-2xx status.",
    )
    response_ms = IntegerField(
        null=True,
        blank=True,
        help_text="Round-trip response time in milliseconds.",
    )
    was_rate_limited = BooleanField(
        default=False,
        help_text="True if this entry records a call that was blocked by rate limiting.",
    )
    was_geo_filtered = BooleanField(
        default=False,
        help_text="True if this entry records a call that was skipped due to geography filtering.",
    )
    was_service_disabled = BooleanField(
        default=False,
        help_text="True if this entry records a call that was skipped due to service being disabled.",
    )
    cost_estimate = DecimalField(
        max_digits=10,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Estimated USD cost of this call, from the service's ServiceDefaults.cost_per_call at call time. Null means no per-call cost is configured for this service (free, or not yet priced) - not necessarily that the call was free.",
    )

    objects = ApiCallLogManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_api_call_log"
        verbose_name = "API Call Log"
        verbose_name_plural = "API Call Logs"
        indexes = [
            # Composite index for rate-limit window queries: service + created
            Index(fields=["service", "created"], name="idxdb_apilog_svc_cdt"),
        ]
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"{self.service} @ {self.created}"

    @classmethod
    def prune_older_than_days(cls, days: int = 90) -> int:
        """Delete log entries older than ``days`` days.

        Args:
            days: Entries older than this many days are deleted. The default
                is safe for rate limiting but NOT for cost reporting - the
                public costs page reconstructs 12 months of API spend from
                these rows, so the scheduled caller
                (``tasks.prune_api_call_logs``) passes 400.

        Returns:
            Number of rows deleted.
        """
        from datetime import timedelta

        from django.utils import timezone

        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = cls.objects.filter(created__lt=cutoff).delete()
        return deleted
