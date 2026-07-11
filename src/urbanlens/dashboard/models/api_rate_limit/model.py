"""ApiRateLimit model - per-service rate limiting configuration."""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import BooleanField, CharField, IntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.api_rate_limit.queryset import ApiRateLimitManager


class ApiRateLimit(abstract.DashboardModel):
    """Rate limiting configuration for one external API service.

    One row per service key (e.g. ``"nps"``, ``"wikipedia"``). Rows are
    auto-created with sensible defaults the first time a service is checked.
    Admins can override limits and toggle geo-filtering from the site-admin UI.
    """

    service = CharField(
        max_length=50,
        unique=True,
        help_text="Internal service identifier (e.g. 'nps', 'google_places').",
    )
    display_name = CharField(
        max_length=100,
        help_text="Human-readable service name shown in the admin UI.",
    )
    enabled = BooleanField(
        default=True,
        help_text="Master toggle. When disabled, all calls to this service are blocked.",
    )
    calls_per_minute = IntegerField(
        null=True,
        blank=True,
        help_text="Maximum calls allowed per rolling minute. Leave blank for no per-minute limit.",
        validators=[MinValueValidator(1), MaxValueValidator(60_000)],
    )
    calls_per_day = IntegerField(
        null=True,
        blank=True,
        help_text="Maximum calls allowed per calendar day (UTC). Leave blank for no daily limit.",
        validators=[MinValueValidator(1), MaxValueValidator(10_000_000)],
    )
    usa_only = BooleanField(
        default=False,
        help_text=("Skip API calls for coordinates outside the United States. Enable for USA-centric services (NPS, LoopNet, Library of Congress, etc.)."),
    )
    notes = TextField(
        blank=True,
        help_text="Optional admin notes (e.g. free-tier limits, billing thresholds).",
    )

    objects = ApiRateLimitManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_api_rate_limit"
        verbose_name = "API Rate Limit"
        verbose_name_plural = "API Rate Limits"
        ordering = ["display_name"]

    def __str__(self) -> str:
        return self.display_name or self.service
