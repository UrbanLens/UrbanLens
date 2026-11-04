"""ProfileTrustSignals - private per-profile inputs to the real-user and independence checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, DateTimeField, JSONField, OneToOneField, PositiveIntegerField

from urbanlens.dashboard.models import abstract


class ProfileTrustSignals(abstract.DashboardModel):
    """
    Cached evidence that a profile is a real, distinct person (ADR35 section 5).

    Never expose a row, or anything derived from it, to another user. One row per profile, rebuilt nightly and
    on demand when stale (``services.trust.signals_for``).

    Attributes:
        profile: Whose signals these are.
        computed_at: When the row was last rebuilt.
        account_age_days: Days since the profile was created.
        active_days_90: Distinct days with any recorded activity in the last 90.
        pin_count: How many pins the profile holds.
        pin_cells: Geohash-4 cells of the profile's pins, most pinned first, capped at 64.
        device_track_visits: Visits recorded by the device's own location track.
        photo_visits: Visits inferred from photos with GPS and time.
        recorded_visits: Visits the user (or an import) merely asserts.
    """

    profile = OneToOneField("dashboard.Profile", on_delete=CASCADE, related_name="trust_signals")
    computed_at = DateTimeField()
    account_age_days = PositiveIntegerField(default=0)
    active_days_90 = PositiveIntegerField(default=0)
    pin_count = PositiveIntegerField(default=0)
    pin_cells = JSONField(default=list)
    device_track_visits = PositiveIntegerField(default=0)
    photo_visits = PositiveIntegerField(default=0)
    recorded_visits = PositiveIntegerField(default=0)

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_trust_signals"

    def __str__(self) -> str:
        return f"trust signals for profile {self.profile_id}"
