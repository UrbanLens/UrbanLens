"""ProfileTrustSignals - private per-profile inputs to the real-user and independence checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, BooleanField, DateTimeField, JSONField, OneToOneField, PositiveIntegerField

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
        active_days_90: Distinct days in the last 90 with a meaningful action (a login alone does not count).
        pin_count: Pins at least 14 days old; fresh pins are cheap to scatter.
        pin_cells: Geohash-6 cells (about 1 km) of those pins, most pinned first, capped at 64.
        region_count: Distinct geohash-4 cells (about 40 km) among those pins.
        vouch_count: Accepted friends whose own row said they were real users when it was last computed.
        is_real_user: The verdict at ``computed_at``; read by other profiles' vouching, so there is no recursion.
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
    region_count = PositiveIntegerField(default=0)
    vouch_count = PositiveIntegerField(default=0)
    is_real_user = BooleanField(default=False)
    device_track_visits = PositiveIntegerField(default=0)
    photo_visits = PositiveIntegerField(default=0)
    recorded_visits = PositiveIntegerField(default=0)

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_trust_signals"

    def __str__(self) -> str:
        return f"trust signals for profile {self.profile_id}"
