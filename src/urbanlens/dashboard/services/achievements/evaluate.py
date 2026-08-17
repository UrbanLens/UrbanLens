"""Deciding which achievements a profile has earned, and granting them.

Single-profile evaluation funnels through :func:`evaluate_profile` - signals
call it with the handful of metric keys their event could have moved. The
nightly sweep instead goes through :func:`evaluate_profiles_in_range`, one
bounded pk-range chunk per Celery task, with the metric values computed in
bulk for the whole chunk.
"""

# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Django Imports
from django.db import IntegrityError, transaction
from django.urls import NoReverseMatch, reverse

# App Imports
from urbanlens.dashboard.services.achievements.metrics import compute_values, compute_values_bulk, get_metric

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from urbanlens.dashboard.models.achievements.model import Achievement, UserAchievement
    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


def active_metric_keys() -> set[str]:
    """Return the metric keys at least one active achievement measures.

    Signals use this to skip queueing work that provably has nothing to do: with
    no award defined against ``comments_written``, posting a comment should not
    cost a Celery message.

    Deliberately uncached. It is one indexed query against a table that holds a
    handful of admin-authored rows, and caching it bought staleness instead of
    speed: a rolled-back transaction leaves no signal to invalidate on, so the
    stale set survives and makes write-path behaviour depend on what ran before
    it.

    Returns:
        The set of measured metric keys, or an empty set if the table cannot be
        read yet (mid-migration on a fresh database).
    """
    from urbanlens.dashboard.models.achievements.model import Achievement

    try:
        return set(Achievement.objects.active().values_list("metric", flat=True).distinct())
    except Exception:
        # The table may not exist yet while migrations are still running; a
        # contribution must not fail because of that.
        logger.debug("Could not read active achievement metrics", exc_info=True)
        return set()


def evaluate_profile(
    profile: Profile,
    metric_keys: Iterable[str] | None = None,
    *,
    notify: bool = True,
) -> list[UserAchievement]:
    """Grant every active achievement *profile* now qualifies for.

    Only metrics that some active achievement actually uses are computed, so
    passing a narrow ``metric_keys`` after a single write is cheap.

    Args:
        profile: The profile to evaluate.
        metric_keys: Restrict evaluation to achievements measuring these
            metrics. None evaluates every active achievement.
        notify: Whether to raise an in-app notification per new award. Turned
            off for bulk backfills of historical activity.

    Returns:
        The awards newly granted by this call, in grant order. Empty when the
        profile already held everything it qualifies for.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement, UserAchievement

    achievements = Achievement.objects.active()
    if metric_keys is not None:
        keys = list(dict.fromkeys(metric_keys))
        if not keys:
            return []
        achievements = achievements.for_metric(keys)

    candidates: Sequence[Achievement] = list(achievements)
    if not candidates:
        return []

    already_earned = UserAchievement.objects.for_profile(profile).earned_achievement_ids()
    pending = [a for a in candidates if a.pk not in already_earned]
    if not pending:
        return []

    values = compute_values(profile, {a.metric for a in pending})
    return _award_qualifying(profile, pending, values, notify=notify)


def _award_qualifying(
    profile: Profile,
    pending: Sequence[Achievement],
    values: Mapping[str, int],
    *,
    notify: bool,
) -> list[UserAchievement]:
    """Grant whichever of *pending* the precomputed *values* qualify *profile* for.

    The shared award loop behind :func:`evaluate_profile` and
    :func:`evaluate_profiles_in_range`, so per-write and sweep evaluation
    cannot drift apart in how a threshold is judged.

    Args:
        profile: The profile being evaluated.
        pending: Active achievements the profile has not already earned.
        values: Metric values keyed by metric key. A missing key is treated as
            not qualifying rather than as zero, so an unknown metric can never
            accidentally grant.
        notify: Whether to raise an in-app notification per new award.

    Returns:
        The awards newly granted, in grant order.
    """
    granted: list[UserAchievement] = []
    for achievement in pending:
        value = values.get(achievement.metric)
        if value is None or value < achievement.threshold:
            continue
        award = _grant(profile, achievement, value)
        if award is not None:
            granted.append(award)

    if notify:
        for award in granted:
            _notify(profile, award)

    return granted


def _grant(profile: Profile, achievement: Achievement, value: int) -> UserAchievement | None:
    """Create the award row, tolerating a concurrent grant of the same award.

    Args:
        profile: Who earned it.
        achievement: What they earned.
        value: The metric value that qualified them.

    Returns:
        The new award, or None when another worker granted it first.
    """
    from urbanlens.dashboard.models.achievements.model import UserAchievement

    try:
        with transaction.atomic():
            award, created = UserAchievement.objects.get_or_create(
                profile=profile,
                achievement=achievement,
                defaults={"value_at_award": value},
            )
    except IntegrityError:
        # Lost a race against another evaluation pass; the award exists either way.
        return None

    if not created:
        return None

    logger.info("Granted achievement %s to profile %s at value %s", achievement.pk, profile.pk, value)
    return award


def _notify(profile: Profile, award: UserAchievement) -> None:
    """Raise an in-app notification for a newly earned award.

    Notification failures must not roll back the award itself, so this swallows
    and logs rather than propagating.
    """
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    # A missing preferences row raises RelatedObjectDoesNotExist, which is an
    # AttributeError subclass. Default to SITE rather than NONE so a user who
    # never opened their settings still hears about this.
    try:
        preference = getattr(profile.notification_preferences, "achievement_earned", DeliveryPreference.SITE)
    except AttributeError:
        preference = DeliveryPreference.SITE
    if preference == DeliveryPreference.NONE:
        return

    achievement = award.achievement
    try:
        url = reverse("profile.view")
    except NoReverseMatch:
        url = None

    try:
        NotificationLog.objects.create(
            profile=profile,
            notification_type=NotificationType.ACHIEVEMENT_EARNED,
            title=f"Achievement unlocked: {achievement.name}",
            message=achievement.description or achievement.requirement_text,
            url=url,
        )
    except Exception:
        logger.exception("Failed to notify profile %s of achievement %s", profile.pk, achievement.pk)


def evaluate_achievement_for_all(achievement: Achievement, *, notify: bool = False) -> int:
    """Grant a single achievement to every profile that already qualifies.

    Run when an admin creates or edits an award, so adding one at any time
    immediately reaches users who earned it long ago.

    Args:
        achievement: The award to backfill.
        notify: Whether to notify each recipient. Off by default - a brand new
            award backfilling across the whole user base would otherwise fan out
            a notification to nearly everyone at once.

    Returns:
        How many profiles received the award.
    """
    from urbanlens.dashboard.models.achievements.model import UserAchievement
    from urbanlens.dashboard.models.profile import Profile

    metric = get_metric(achievement.metric)
    if metric is None:
        logger.warning("Skipping backfill of achievement %s: unknown metric %s", achievement.pk, achievement.metric)
        return 0
    if not achievement.is_active:
        return 0

    already = set(UserAchievement.objects.filter(achievement=achievement).values_list("profile_id", flat=True))

    granted = 0
    for profile in Profile.objects.exclude(pk__in=already).iterator():
        value = metric.value_for(profile)
        if value < achievement.threshold:
            continue
        award = _grant(profile, achievement, value)
        if award is None:
            continue
        granted += 1
        if notify:
            _notify(profile, award)

    logger.info("Backfilled achievement %s to %s profiles", achievement.pk, granted)
    return granted


def evaluate_profiles_in_range(start_pk: int, end_pk: int, *, notify: bool = True) -> int:
    """Re-evaluate every active achievement for one pk-range chunk of profiles.

    One chunk of the nightly safety net, which catches awards no write touches
    - a "trips attended" threshold crossed simply because a trip's end date
    passed - and anything an enqueue lost when the broker was down.
    ``tasks.sweep_achievements`` slices the profile table into bounded ranges
    and runs each through this in its own Celery task, so no single invocation
    can approach the hard task time limit and a crashed chunk affects only its
    own range until the next nightly dispatch re-covers it. (This replaced a
    single resumable-cursor task over all profiles; the chunking makes the
    cursor's crash-recovery job redundant.)

    The chunk is priced in metrics, not profiles: values come from
    :func:`~urbanlens.dashboard.services.achievements.metrics.compute_values_bulk`,
    which computes each metric for the whole chunk in a constant number of
    grouped queries. A chunk therefore costs on the order of the active metric
    count in queries, plus a few per award actually granted.

    Args:
        start_pk: Lowest profile pk in the chunk, inclusive.
        end_pk: Highest profile pk in the chunk, inclusive.
        notify: Whether to notify recipients of new awards.

    Returns:
        Total number of awards granted across the chunk.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement, UserAchievement
    from urbanlens.dashboard.models.profile import Profile

    achievements: Sequence[Achievement] = list(Achievement.objects.active())
    if not achievements:
        return 0

    profiles = list(Profile.objects.filter(pk__gte=start_pk, pk__lte=end_pk).order_by("pk"))
    if not profiles:
        return 0

    values_by_profile = compute_values_bulk(profiles, {a.metric for a in achievements})

    earned_by_profile: dict[int, set[int]] = {}
    earned_rows = UserAchievement.objects.filter(profile_id__in=[p.pk for p in profiles]).values_list(
        "profile_id",
        "achievement_id",
    )
    for profile_id, achievement_id in earned_rows:
        earned_by_profile.setdefault(profile_id, set()).add(achievement_id)

    granted = 0
    for profile in profiles:
        already_earned = earned_by_profile.get(profile.pk, set())
        pending = [a for a in achievements if a.pk not in already_earned]
        if not pending:
            continue
        granted += len(_award_qualifying(profile, pending, values_by_profile[profile.pk], notify=notify))
    return granted


def progress_for_profile(profile: Profile, viewer: Profile | None = None) -> list[dict[str, object]]:
    """Return the full award catalogue annotated with one profile's standing.

    Args:
        profile: The profile whose progress is being described.
        viewer: Who is looking. Secret awards are listed only when *profile*
            has earned them, and locked awards' progress bars are shown only to
            the owner - a stranger should not be able to read off exactly how
            many pins someone has.

    Returns:
        One dict per listed achievement with ``achievement``, ``earned``,
        ``earned_at``, ``value`` and ``percent`` keys, ordered by the model's
        default ordering.
    """
    from urbanlens.dashboard.models.achievements.model import Achievement, UserAchievement

    is_owner = viewer is not None and viewer.pk == profile.pk

    catalogue = list(Achievement.objects.active().listable_for(profile))
    if not catalogue:
        return []

    earned = {award.achievement_id: award for award in UserAchievement.objects.for_profile(profile)}
    values = compute_values(profile, {a.metric for a in catalogue}) if is_owner else {}

    rows: list[dict[str, object]] = []
    for achievement in catalogue:
        award = earned.get(achievement.pk)
        value = award.value_at_award if award else values.get(achievement.metric, 0)
        rows.append(
            {
                "achievement": achievement,
                "earned": award is not None,
                "earned_at": award.earned_at if award else None,
                "value": value,
                "percent": 100 if award else achievement.progress_for(value),
                "show_progress": is_owner and award is None,
            },
        )
    return rows


__all__ = [
    "evaluate_achievement_for_all",
    "evaluate_profile",
    "evaluate_profiles_in_range",
    "progress_for_profile",
]
