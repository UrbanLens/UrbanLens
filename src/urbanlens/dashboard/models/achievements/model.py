# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Django Imports
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    ForeignKey,
    ImageField,
    Index,
    IntegerField,
    PositiveIntegerField,
    TextField,
    UniqueConstraint,
)
from django.utils import timezone

# App Imports
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.achievements.meta import ActivityKind
from urbanlens.dashboard.models.achievements.queryset import (
    AchievementManager,
    ProfileActivityDayManager,
    ProfileStreakManager,
    UserAchievementManager,
)

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.profile import Profile
    from urbanlens.dashboard.services.achievements.metrics import Metric

logger = logging.getLogger(__name__)

#: Icons are stored the same way labels store them - either a Material Symbols
#: name or a literal emoji - so the existing icon picker and the
#: ``is_material_icon`` template filter work unchanged.
DEFAULT_ACHIEVEMENT_ICON = "trophy"
DEFAULT_ACHIEVEMENT_COLOR = "#FFC107"


def metric_choices() -> list[tuple[str, str]]:
    """Return ``(key, label)`` pairs for every registered achievement metric.

    Passed to the ``metric`` field as a callable so that registering a new
    metric does not generate a migration, and so importing this module does not
    drag in every model the registry queries.
    """
    from urbanlens.dashboard.services.achievements.metrics import metric_choices as registry_choices

    return registry_choices()


class Achievement(abstract.PublicDashboardModel):
    """An award a site admin defines, earned by passing a threshold on one metric.

    Achievements are data, not code: an admin picks one of the registered
    metrics (see ``services.achievements.metrics``), a threshold, and an icon.
    Adding a new achievement therefore needs no deploy - a ``post_save`` hook
    backfills it against every existing profile so users who already qualify
    receive it immediately.

    Tiers ("10 pins", "100 pins", "1000 pins") are just several achievements
    sharing a metric with different thresholds; there is no separate tier field.

    Attributes:
        name: Display name, e.g. "Cartographer".
        description: One or two sentences shown under the name.
        metric: Registry key of the metric this award measures.
        threshold: The value of ``metric`` at which the award is earned.
        icon: Material Symbols name or emoji, used when ``custom_icon`` is unset.
        custom_icon: Uploaded image, which takes precedence over ``icon``.
        color: Accent colour for the award's badge.
        order: Manual display ordering; lower sorts first.
        is_active: Retired awards stay earned but stop being granted or listed.
        is_secret: Hidden from users who have not earned it yet.
    """

    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    metric = CharField(max_length=64, choices=metric_choices, db_index=True)
    threshold = PositiveIntegerField(default=1)

    # Left unconstrained, like Label.icon: an admin naming a new award should
    # not be limited to whichever icons the picker happens to list.
    icon = CharField(max_length=50, null=True, blank=True, default=DEFAULT_ACHIEVEMENT_ICON)
    custom_icon = ImageField(upload_to="achievement_icons/", null=True, blank=True)
    color = CharField(
        max_length=50,
        null=True,
        blank=True,
        default=DEFAULT_ACHIEVEMENT_COLOR,
        validators=[RegexValidator(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", "Enter a hex colour such as #FFC107.")],
    )

    order = IntegerField(default=0)
    is_active = BooleanField(default=True)
    is_secret = BooleanField(default=False)

    objects = AchievementManager()

    if TYPE_CHECKING:
        awards: object

    class Meta(abstract.PublicDashboardModel.Meta):
        db_table = "dashboard_achievements"
        ordering = ["order", "metric", "threshold", "name"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["metric", "threshold"], name="idxdb_achv_metric_thresh"),
            Index(fields=["is_active"], name="idxdb_achv_active"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.metric} >= {self.threshold})"

    def _slugify_base(self) -> str:
        return self.name or "achievement"

    def clean(self) -> None:
        """Reject metrics that no longer exist in the registry.

        A metric can disappear when a plugin that registered it is removed, so
        this is validated on save rather than being assumed from ``choices``.
        """
        super().clean()
        from urbanlens.dashboard.services.achievements.metrics import get_metric

        if self.metric and get_metric(self.metric) is None:
            raise ValidationError({"metric": f"Unknown achievement metric: {self.metric!r}"})
        if self.threshold < 1:
            raise ValidationError({"threshold": "Threshold must be at least 1."})

    @property
    def metric_definition(self) -> Metric | None:
        """Return the registered :class:`Metric` this award measures, if it still exists."""
        from urbanlens.dashboard.services.achievements.metrics import get_metric

        return get_metric(self.metric)

    @property
    def metric_label(self) -> str:
        """Human-readable name of the underlying metric, for admin lists and tooltips."""
        definition = self.metric_definition
        return definition.label if definition else self.metric

    @property
    def requirement_text(self) -> str:
        """Return a sentence describing what earns this award, e.g. "Pin 25 spots"."""
        definition = self.metric_definition
        if definition is None:
            return f"Reach {self.threshold} of {self.metric}"
        return definition.requirement(self.threshold)

    def progress_for(self, value: int) -> int:
        """Return percent progress toward this award for a current metric *value*.

        Args:
            value: The profile's current value for this award's metric.

        Returns:
            An integer 0-100, clamped.
        """
        if self.threshold <= 0:
            return 100
        return max(0, min(100, round(value * 100 / self.threshold)))


class UserAchievement(abstract.FrontendDashboardModel):
    """One award earned by one profile.

    Awards are permanent: lowering a metric (deleting pins, say) never revokes
    one. Re-earning is prevented by the uniqueness constraint, which is also
    what makes concurrent evaluation safe - two workers racing to grant the same
    award both call ``get_or_create`` and only one row results.

    Attributes:
        profile: Who earned it.
        achievement: What they earned.
        earned_at: When it was granted; set explicitly so backfills can
            attribute an award to the past rather than to the backfill run.
        value_at_award: The metric value at the moment of granting, so the
            profile page can show "42 pins" rather than just the threshold.
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="achievements",
    )
    achievement = ForeignKey(
        "dashboard.Achievement",
        on_delete=CASCADE,
        related_name="awards",
    )
    earned_at = DateTimeField(default=timezone.now, db_index=True)
    value_at_award = PositiveIntegerField(default=0)

    objects = UserAchievementManager()

    if TYPE_CHECKING:
        profile_id: int
        achievement_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_user_achievements"
        ordering = ["-earned_at"]
        get_latest_by = "earned_at"
        constraints = [
            UniqueConstraint(fields=["profile", "achievement"], name="uniq_profile_achievement"),
        ]
        indexes = [
            Index(fields=["profile", "-earned_at"], name="idxdb_uachv_profile_date"),
        ]

    def __str__(self) -> str:
        return f"{self.profile_id} earned {self.achievement_id}"


class ProfileActivityDay(abstract.DashboardModel):
    """A single calendar day on which a profile performed one kind of action.

    This is the source of truth behind streaks. Streak lengths are also cached
    on :class:`ProfileStreak` so reading them is a single indexed row rather
    than a scan, but they can always be rebuilt from these rows.

    Attributes:
        profile: Who acted.
        kind: Which :class:`ActivityKind` they performed.
        day: The local calendar date of the action.
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="activity_days",
    )
    kind = CharField(max_length=20, choices=ActivityKind.choices, db_index=True)
    day = DateField()

    objects = ProfileActivityDayManager()

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_activity_days"
        ordering = ["-day"]
        constraints = [
            UniqueConstraint(fields=["profile", "kind", "day"], name="uniq_profile_activity_day"),
        ]
        indexes = [
            Index(fields=["profile", "kind", "-day"], name="idxdb_pactday_pfile_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.profile_id} {self.kind} on {self.day}"


class ProfileStreak(abstract.DashboardModel):
    """Cached current and longest run of consecutive days for one activity kind.

    ``longest_length`` is what achievements compare against, so a streak that is
    broken later still keeps whatever it earned. ``current_length`` only ever
    advances here - nothing fires on the day a user *stops* acting - so read it
    through :meth:`current_length_as_of`, which treats a stale run as ended.

    Attributes:
        profile: Whose streak this is.
        kind: Which :class:`ActivityKind` is being tracked.
        current_length: Days in the run ending at ``last_day``.
        longest_length: Best run ever recorded.
        last_day: The most recent day counted toward ``current_length``.
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="streaks",
    )
    kind = CharField(max_length=20, choices=ActivityKind.choices, db_index=True)
    current_length = PositiveIntegerField(default=0)
    longest_length = PositiveIntegerField(default=0)
    last_day = DateField(null=True, blank=True)

    objects = ProfileStreakManager()

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_streaks"
        ordering = ["kind"]
        constraints = [
            UniqueConstraint(fields=["profile", "kind"], name="uniq_profile_streak_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.profile_id} {self.kind}: {self.current_length} (best {self.longest_length})"

    def current_length_as_of(self, today: datetime.date | None = None) -> int:
        """Return the live current streak, treating a missed day as a reset.

        Args:
            today: The date to evaluate against; defaults to the local date.

        Returns:
            ``current_length`` when the run is still alive (``last_day`` is
            today or yesterday), otherwise 0.
        """
        if self.last_day is None:
            return 0
        today = today or timezone.localdate()
        if (today - self.last_day).days <= 1:
            return self.current_length
        return 0

    @property
    def is_active_today(self) -> bool:
        """True when this streak has already been extended on the current local date."""
        return self.last_day == timezone.localdate()
