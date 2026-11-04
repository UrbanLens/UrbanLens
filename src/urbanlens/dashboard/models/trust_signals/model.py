"""ProfileTrustSignals - private per-profile inputs to the real-user and independence checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, BooleanField, DateTimeField, FloatField, JSONField, OneToOneField, PositiveIntegerField

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
        active_weeks_90: Distinct ISO weeks in the last 90 days containing a meaningful action (a login alone does not count).
        pin_count: Pins at least 14 days old; fresh pins are cheap to scatter.
        pin_cells: Geohash-6 cells (about 1 km) of pins from the last ``UL_TRUST_CELL_WINDOW_DAYS`` days, most pinned first, capped at 64.
        spread_cells: Distinct geohash-5 cells (about 5 km) among the mature pins.
        trusted_friend_count: Friends of at least 30 days whose ``base_score`` cleared the threshold at their last computation.
        base_score: The real-user score without the trusted-friend part, which is what the trusted-friend count reads, so it never feeds back on itself.
        is_real_user: The verdict at ``computed_at``; informational; the trusted-friend count reads only base_score.
        claim_visit_days: Days with a visit the user or a client merely asserts (geolocation post, journal, import, trip, friend's journal).
        photo_visit_days: Days with a visit inferred from a photo with GPS and time.
    """

    profile = OneToOneField("dashboard.Profile", on_delete=CASCADE, related_name="trust_signals")
    computed_at = DateTimeField()
    account_age_days = PositiveIntegerField(default=0)
    active_weeks_90 = PositiveIntegerField(default=0)
    pin_count = PositiveIntegerField(default=0)
    pin_cells = JSONField(default=list)
    spread_cells = PositiveIntegerField(default=0)
    trusted_friend_count = PositiveIntegerField(default=0)
    base_score = FloatField(default=0.0)
    is_real_user = BooleanField(default=False)
    claim_visit_days = PositiveIntegerField(default=0)
    photo_visit_days = PositiveIntegerField(default=0)

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_trust_signals"

    def __str__(self) -> str:
        return f"trust signals for profile {self.profile_id}"
