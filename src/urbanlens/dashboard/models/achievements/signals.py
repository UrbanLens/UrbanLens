"""Signal wiring that keeps achievements up to date as users contribute.

Every handler does the same two things - work out whose totals changed, and
hand off to Celery - so they are generated from :data:`_SUBSCRIPTIONS` rather
than written out one by one.

Awards are never revoked, so there are deliberately no ``post_delete``
handlers: deleting a pin lowers the metric but keeps whatever it earned.
"""

# Generic imports
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

# Django Imports
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.db.models.signals import post_save

# App Imports
from urbanlens.dashboard.models.achievements.meta import ActivityKind
from urbanlens.dashboard.services.achievements import metrics as metric_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Model

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Subscription:
    """How one source model feeds the achievement system.

    Attributes:
        model_path: ``module:ClassName`` of the sender, resolved lazily so this
            module can be imported before the app registry is ready.
        profile_ids: Returns the profile PKs whose totals the saved instance
            could have changed.
        triggers: Trigger names to translate into affected metric keys.
        activity_kind: Streak bucket to record, when this event is one.
        activity_when: Extra test the instance must pass before it counts toward
            the streak. Needed where the streak is narrower than the trigger -
            every ``Image`` row invalidates the photo count, but only a genuine
            upload should extend the photo streak.
        created_only: Skip updates, only react to inserts. Off where a later
            edit matters (a wiki being promoted out of draft, an invitation
            being accepted).
    """

    model_path: str
    profile_ids: Callable[[Any], list[int]]
    triggers: frozenset[str]
    activity_kind: str | None = None
    activity_when: Callable[[Any], bool] | None = None
    created_only: bool = True


def _attr_ids(*attrs: str) -> Callable[[Any], list[int]]:
    """Return a profile-id getter reading one or more FK id attributes."""

    def getter(instance: Any) -> list[int]:
        return [value for attr in attrs if (value := getattr(instance, attr, None)) is not None]

    return getter


def _is_genuine_upload(instance: Any) -> bool:
    """True when an Image row is the user's own upload, not an external photo.

    Attaching a Yelp or Wikimedia photo creates an ``Image`` too, and that must
    not extend a photo-upload streak.
    """
    from urbanlens.dashboard.models.images.model import ImageSource

    return instance.source == ImageSource.UPLOAD


def _visit_profile_ids(instance: Any) -> list[int]:
    """Return the owner of the pin a visit was logged against."""
    from urbanlens.dashboard.models.pin.model import Pin

    if instance.pin_id is None:
        return []
    profile_id = Pin.objects.filter(pk=instance.pin_id).values_list("profile_id", flat=True).first()
    return [profile_id] if profile_id is not None else []


_SUBSCRIPTIONS: tuple[_Subscription, ...] = (
    _Subscription(
        model_path="urbanlens.dashboard.models.pin.model:Pin",
        profile_ids=_attr_ids("profile_id"),
        # Not created_only: vulnerability/danger are rated by editing the pin.
        triggers=frozenset({metric_registry.TRIGGER_PIN}),
        activity_kind=ActivityKind.PIN,
        created_only=False,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.images.model:Image",
        profile_ids=_attr_ids("profile_id"),
        triggers=frozenset({metric_registry.TRIGGER_PHOTO}),
        activity_kind=ActivityKind.PHOTO,
        activity_when=_is_genuine_upload,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.comments.model:Comment",
        profile_ids=_attr_ids("profile_id"),
        triggers=frozenset({metric_registry.TRIGGER_COMMENT}),
        activity_kind=ActivityKind.COMMENT,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.trips.model:TripComment",
        profile_ids=_attr_ids("author_id"),
        triggers=frozenset({metric_registry.TRIGGER_COMMENT}),
        activity_kind=ActivityKind.COMMENT,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.wiki_edit.model:WikiEdit",
        profile_ids=_attr_ids("editor_id"),
        triggers=frozenset({metric_registry.TRIGGER_WIKI_EDIT}),
        activity_kind=ActivityKind.WIKI_EDIT,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.reviews.model:Review",
        profile_ids=_attr_ids("profile_id"),
        triggers=frozenset({metric_registry.TRIGGER_REVIEW}),
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.friendship.model:Friendship",
        profile_ids=_attr_ids("from_profile_id", "to_profile_id"),
        # Not created_only: a friendship counts when it reaches ACCEPTED, which
        # is an update to the row the request created.
        triggers=frozenset({metric_registry.TRIGGER_FRIENDSHIP}),
        created_only=False,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.trips.model:Trip",
        profile_ids=_attr_ids("creator_id"),
        triggers=frozenset({metric_registry.TRIGGER_TRIP}),
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.trips.model:TripMembership",
        profile_ids=_attr_ids("profile_id"),
        triggers=frozenset({metric_registry.TRIGGER_TRIP_MEMBERSHIP}),
        created_only=False,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.markup.model:MarkupMap",
        profile_ids=_attr_ids("profile_id"),
        triggers=frozenset({metric_registry.TRIGGER_MARKUP_MAP}),
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.friendship.invitation.model:FriendInvitation",
        profile_ids=_attr_ids("inviter_id"),
        # Not created_only: only an accepted invitation counts, and acceptance
        # is an update.
        triggers=frozenset({metric_registry.TRIGGER_INVITATION}),
        created_only=False,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.visits.model:PinVisit",
        profile_ids=_visit_profile_ids,
        triggers=frozenset({metric_registry.TRIGGER_VISIT}),
    ),
)


def _record_streak_days(profile_ids: list[int], activity_kind: str) -> bool:
    """Record today's activity day for each profile, synchronously.

    Deliberately not deferred to Celery. Streaks are the only metric with no
    source of truth outside our own tables, so the day has to be written even
    when no streak award exists yet - otherwise an award added next month would
    have no history to reward. Writing it inside the caller's transaction also
    means a rolled-back contribution rolls back its streak day with it.

    The cost is bounded: one indexed ``get_or_create`` that hits at most once
    per profile per kind per day.

    Args:
        profile_ids: Profiles that performed the action.
        activity_kind: The :class:`ActivityKind` performed.

    Returns:
        True when any profile's streak actually advanced.
    """
    from urbanlens.dashboard.services.achievements.activity import record_activity

    advanced = False
    for profile_id in profile_ids:
        advanced |= record_activity(profile_id, activity_kind)
    return advanced


def _schedule(profile_ids: list[int], metric_keys: list[str], activity_kind: str | None) -> None:
    """Record any streak day, then queue evaluation if an award depends on it.

    The enqueue is gated on :func:`active_metric_keys` so a site that has not
    defined an award against a metric pays nothing when that metric changes -
    no broker message, no worker time. The nightly sweep still backstops
    everything.

    The enqueue itself is deferred to ``on_commit``: firing it inline would
    queue work against rows that may still roll back, and would run against
    invisible data in eager mode.
    """
    from urbanlens.dashboard.services.achievements.evaluate import active_metric_keys

    if not profile_ids:
        return

    profile_ids = list(dict.fromkeys(profile_ids))

    affected = set(metric_keys)
    if activity_kind is not None and _record_streak_days(profile_ids, activity_kind):
        affected.update(metric_registry.metrics_for_triggers({metric_registry.TRIGGER_STREAK}))

    measured = sorted(affected & active_metric_keys())
    if not measured:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import evaluate_achievements_for_profile

        for profile_id in profile_ids:
            safely_enqueue_task(evaluate_achievements_for_profile, profile_id, metric_keys=measured)

    transaction.on_commit(_enqueue)


def _make_handler(subscription: _Subscription) -> Callable[..., None]:
    """Build the ``post_save`` receiver for one subscription."""
    metric_keys = metric_registry.metrics_for_triggers(subscription.triggers)

    def handler(sender: type[Model], instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
        # `raw` is set during loaddata, when related rows may not exist yet.
        if raw or (subscription.created_only and not created):
            return

        # Only an insert is an "action taken today"; editing a pin months later
        # is not a new day of the pinning streak.
        kind = subscription.activity_kind if created else None
        if kind is not None and subscription.activity_when is not None and not subscription.activity_when(instance):
            kind = None

        _schedule(subscription.profile_ids(instance), metric_keys, kind)

    handler.__name__ = f"achievements_on_{subscription.model_path.rsplit(':', 1)[-1].lower()}_save"
    return handler


def _resolve(model_path: str) -> type[Model]:
    """Import and return the model class named by ``module:ClassName``."""
    from importlib import import_module

    module_name, class_name = model_path.split(":")
    return getattr(import_module(module_name), class_name)


def on_user_logged_in(sender: object, user: Any, **kwargs: Any) -> None:
    """Record a login-streak day for the profile that just signed in."""
    from urbanlens.dashboard.models.profile import Profile

    profile_id = Profile.objects.filter(user=user).values_list("pk", flat=True).first()
    if profile_id is None:
        return
    _schedule([profile_id], metric_registry.metrics_for_triggers({metric_registry.TRIGGER_STREAK}), ActivityKind.LOGIN)


def on_achievement_saved(sender: type[Model], instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
    """Backfill a newly defined or re-activated award across every profile.

    This is what lets an admin add an award at any time and have users who
    already qualify receive it, rather than only rewarding future activity.
    """
    if raw or not instance.is_active:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import backfill_achievement

        safely_enqueue_task(backfill_achievement, instance.pk)

    transaction.on_commit(_enqueue)


def connect() -> None:
    """Connect every achievement signal. Called from ``DashboardConfig.ready``."""
    from urbanlens.dashboard.models.achievements.model import Achievement

    for index, subscription in enumerate(_SUBSCRIPTIONS):
        try:
            model = _resolve(subscription.model_path)
        except (ImportError, AttributeError):
            logger.exception("Could not connect achievement signal for %s", subscription.model_path)
            continue
        post_save.connect(
            _make_handler(subscription),
            sender=model,
            # Keyed on the subscription, not just its model. Django dedupes by
            # (dispatch_uid, sender), so a model-only uid means a second
            # subscription for a model already listed silently replaces the
            # first instead of adding to it - one of the two sets of triggers
            # would just stop firing, with nothing to notice it. The index still
            # gives each subscription one stable uid, so reconnecting stays
            # idempotent.
            dispatch_uid=f"achievement_subscription_{index}_{model._meta.label_lower}",  # noqa: SLF001
            weak=False,
        )

    user_logged_in.connect(on_user_logged_in, dispatch_uid="achievements_user_logged_in")
    post_save.connect(on_achievement_saved, sender=Achievement, dispatch_uid="achievements_backfill_on_define")
