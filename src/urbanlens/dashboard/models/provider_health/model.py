"""ProviderHealth model - one row per external provider this deployment calls, and how it is doing."""

from __future__ import annotations

from django.db.models import CharField, DateTimeField, FloatField, PositiveIntegerField, PositiveSmallIntegerField, TextField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.provider_health.meta import BackoffCause, ProviderState
from urbanlens.dashboard.models.provider_health.queryset import ProviderHealthManager


class ProviderHealth(abstract.DashboardModel):
    """What ``services.core.provider_health`` last concluded about one provider.

    Written only by that module's evaluator, every five minutes, from ``ApiCallLog``. The call log is the record;
    this row is the verdict, the backoff in force and what a person has been told, kept in the database rather than
    the cache so a cache flush cannot end a backoff or repeat an alert.
    """

    provider = CharField(max_length=50, unique=True, help_text="The rate-limiter service key the provider's calls are logged under.")
    state = CharField(max_length=16, choices=ProviderState.choices, default=ProviderState.HEALTHY)
    cause = CharField(max_length=16, choices=BackoffCause.choices, blank=True, default=BackoffCause.NONE)
    reason = TextField(blank=True, default="", help_text="The counts behind the last verdict, in one sentence a person can act on.")
    level = PositiveSmallIntegerField(default=0, help_text="Consecutive backoffs, which set the next one's length. Back to 0 after a day healthy.")
    state_since = DateTimeField(default=timezone.now)
    episode_started_at = DateTimeField(null=True, blank=True, help_text="When the provider last left healthy; null while healthy.")
    backed_off_until = DateTimeField(null=True, blank=True)
    counted_from = DateTimeField(null=True, blank=True, help_text="Calls before this are not judged, so the failures that backed a provider off cannot back it off again once it recovers.")
    last_evaluated_at = DateTimeField(null=True, blank=True)
    last_attempt_at = DateTimeField(null=True, blank=True)
    last_ok_at = DateTimeField(null=True, blank=True)
    window_minutes = PositiveIntegerField(default=0, help_text="The window the last verdict was read from.")
    attempts = PositiveIntegerField(default=0)
    answered = PositiveIntegerField(default=0)
    empty = PositiveIntegerField(default=0)
    refused = PositiveIntegerField(default=0)
    failed = PositiveIntegerField(default=0)
    failure_status = PositiveSmallIntegerField(null=True, blank=True, help_text="The commonest HTTP status among that window's failures.")
    baseline_attempts = PositiveIntegerField(default=0, help_text="Calls over the 7 days ending a day ago, from which the provider's normal is read.")
    baseline_answered_share = FloatField(null=True, blank=True)
    baseline_empty_share = FloatField(null=True, blank=True)
    baseline_computed_at = DateTimeField(null=True, blank=True)
    alerted_at = DateTimeField(null=True, blank=True, help_text="When a person was last told about this provider's current episode.")

    objects = ProviderHealthManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_provider_health"
        verbose_name = "Provider health"
        verbose_name_plural = "Provider health"
        ordering = ["provider"]

    def __str__(self) -> str:
        return f"{self.provider} ({self.get_state_display()})"
