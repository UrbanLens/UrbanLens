"""The catalogue of things an achievement can be defined against.

A metric is a named, countable query over one profile's contributions. Site
admins pick a metric plus a threshold when they create an
:class:`~urbanlens.dashboard.models.achievements.model.Achievement`, so the set
of metrics is the vocabulary the achievement system understands.

Metrics are registered rather than hard-coded into a match statement so that a
plugin can contribute its own (see :func:`register`). ``Achievement.metric``
takes its ``choices`` from :func:`metric_choices` as a callable, which keeps
registering a metric from generating a migration.

Every ``compute`` callable imports its models inside the function body: this
module is imported from ``models.achievements.model``, so importing the models
at the top would be circular.
"""

# Generic imports
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, TypedDict

# Django Imports
from django.db.models import Count, F, Q
from django.utils import timezone

# App Imports
from urbanlens.dashboard.models.achievements.meta import ActivityKind, streak_metric_key

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    import datetime

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)

#: Grouping labels used to organise the metric dropdown in the admin UI.
GROUP_CONTENT = "Content"
GROUP_EXPLORATION = "Exploration"
GROUP_COMMUNITY = "Community"
GROUP_STREAKS = "Streaks"


class StreakSummaryRow(TypedDict):
    """One :class:`ActivityKind`'s streak state, as returned by :func:`streak_summary`."""

    kind: str
    label: str
    current: int
    longest: int


@dataclass(frozen=True)
class Metric:
    """One countable dimension of a profile's contribution.

    Attributes:
        key: Stable identifier stored on ``Achievement.metric``. Never rename
            one without a data migration - existing awards point at it.
        label: Short admin-facing name, e.g. "Pins created".
        unit: Plural noun for the counted thing, e.g. "pins". Used to render
            progress ("42 / 100 pins").
        description: What the metric counts, including anything it excludes.
        compute: Returns the metric's current value for a profile.
        compute_bulk: Returns the metric's value for many profiles at once,
            keyed by profile pk, in a constant number of grouped queries.
            Optional; a metric without one is computed per profile via
            ``compute``. Profile pks the mapping omits read as 0, so an
            implementation only needs to report profiles with a non-zero
            value. Must agree with ``compute`` for every profile.
        group: Which section of the admin dropdown this belongs to.
        triggers: Activity events that can change this metric. Signals pass an
            event name and only the metrics listing it are recomputed, so a new
            pin does not re-run the comment count.
        requirement_template: Sentence describing how to earn the award, with
            ``{threshold}`` substituted in.
    """

    key: str
    label: str
    unit: str
    description: str
    compute: Callable[[Profile], int]
    compute_bulk: Callable[[Sequence[int]], dict[int, int]] | None = None
    group: str = GROUP_CONTENT
    triggers: frozenset[str] = field(default_factory=frozenset)
    requirement_template: str = "Reach {threshold}"

    def requirement(self, threshold: int) -> str:
        """Return the human-readable requirement sentence for a threshold."""
        return self.requirement_template.format(threshold=threshold)

    def value_for(self, profile: Profile) -> int:
        """Return this metric's current value for *profile*, never raising.

        A metric that blows up (a plugin's model went away, say) must not take
        down the whole evaluation pass, so failures are logged and read as 0.
        """
        try:
            return int(self.compute(profile))
        except Exception:
            logger.exception("Achievement metric %s failed for profile %s", self.key, getattr(profile, "pk", None))
            return 0

    def values_for_many(self, profiles: Sequence[Profile]) -> dict[int, int]:
        """Return this metric's current value for every profile, never raising.

        Uses :attr:`compute_bulk` when the metric provides one, so a chunk of
        profiles costs a constant number of queries instead of one per
        profile. A metric without a bulk form - or whose bulk form raises -
        falls back to :meth:`value_for` per profile, which keeps the failure
        surface identical to per-profile evaluation rather than zeroing a
        whole chunk at once.

        Args:
            profiles: The profiles to measure.

        Returns:
            Mapping of profile pk to current value. Every pk in *profiles* is
            present; ones the bulk query did not mention read as 0.
        """
        if self.compute_bulk is not None:
            profile_ids = [profile.pk for profile in profiles]
            try:
                computed = self.compute_bulk(profile_ids)
                return {pk: int(computed.get(pk, 0)) for pk in profile_ids}
            except Exception:
                logger.exception("Achievement metric %s failed in bulk; falling back to per-profile", self.key)
        return {profile.pk: self.value_for(profile) for profile in profiles}


_METRICS: dict[str, Metric] = {}


def register(metric: Metric) -> Metric:
    """Add *metric* to the registry, replacing any metric with the same key.

    Args:
        metric: The metric to register.

    Returns:
        The registered metric, so this can be used as a decorator-ish helper.
    """
    if metric.key in _METRICS and _METRICS[metric.key] is not metric:
        logger.info("Replacing already-registered achievement metric %s", metric.key)
    _METRICS[metric.key] = metric
    return metric


def get_metric(key: str) -> Metric | None:
    """Return the metric registered under *key*, or None when it is unknown."""
    return _METRICS.get(key)


def all_metrics() -> list[Metric]:
    """Return every registered metric, ordered by group then label."""
    order = [GROUP_CONTENT, GROUP_EXPLORATION, GROUP_COMMUNITY, GROUP_STREAKS]
    return sorted(
        _METRICS.values(),
        key=lambda m: (order.index(m.group) if m.group in order else len(order), m.label),
    )


def metric_choices() -> list[tuple[str, str]]:
    """Return ``(key, label)`` pairs for use as Django field choices."""
    return [(m.key, m.label) for m in all_metrics()]


def grouped_metric_choices() -> list[tuple[str, list[tuple[str, str]]]]:
    """Return choices nested by group, for an ``<optgroup>``-style dropdown."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    for metric in all_metrics():
        grouped.setdefault(metric.group, []).append((metric.key, metric.label))
    return list(grouped.items())


def metrics_for_triggers(events: Iterable[str]) -> list[str]:
    """Return the keys of metrics that any of *events* could have changed.

    Args:
        events: Trigger names emitted by signal handlers.

    Returns:
        The affected metric keys. An unrecognised event matches nothing, which
        is safe because the nightly sweep re-evaluates everything anyway.
    """
    wanted = set(events)
    return [m.key for m in _METRICS.values() if m.triggers & wanted]


# ---------------------------------------------------------------------------
# Trigger names. Signals emit these; metrics subscribe to them.
# ---------------------------------------------------------------------------

TRIGGER_PIN = "pin"
TRIGGER_WIKI_EDIT = "wiki_edit"
TRIGGER_PHOTO = "photo"
TRIGGER_VISIT = "visit"
TRIGGER_REVIEW = "review"
TRIGGER_FRIENDSHIP = "friendship"
TRIGGER_TRIP = "trip"
TRIGGER_TRIP_MEMBERSHIP = "trip_membership"
TRIGGER_COMMENT = "comment"
TRIGGER_MARKUP_MAP = "markup_map"
TRIGGER_INVITATION = "invitation"
TRIGGER_STREAK = "streak"


# ---------------------------------------------------------------------------
# Metric implementations. Each countable metric has two forms that must agree:
# ``_x(profile)`` for signal-driven single-profile checks, and ``_x_bulk(ids)``
# for the nightly sweep, which prices a whole chunk of profiles at a constant
# number of grouped queries instead of one query per profile.
# ---------------------------------------------------------------------------


def _grouped_count(queryset: QuerySet, group_field: str, count_field: str = "id", *, distinct: bool = False) -> dict[int, int]:
    """Collapse *queryset* to a ``{group_field value: row count}`` mapping.

    Args:
        queryset: Rows to count, already filtered to the profiles of interest.
        group_field: Field (or related lookup) holding the profile pk.
        count_field: Field counted within each group.
        distinct: Whether to count distinct ``count_field`` values only.

    Returns:
        Mapping of profile pk to count. Groups with no rows are absent.
    """
    rows = queryset.values(group_field).annotate(_bulk_count=Count(count_field, distinct=distinct))
    return {row[group_field]: row["_bulk_count"] for row in rows}


def _pins_created(profile: Profile) -> int:
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.filter(profile=profile).count()


def _pins_created_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.pin.model import Pin

    return _grouped_count(Pin.objects.filter(profile_id__in=profile_ids), "profile_id")


def _photos_uploaded(profile: Profile) -> int:
    from urbanlens.dashboard.models.images.model import Image, ImageSource

    # Only genuine uploads: rows materialised from Yelp/Wikimedia/etc. are
    # someone else's photo that this profile merely attached.
    return Image.objects.filter(profile=profile, source=ImageSource.UPLOAD).count()


def _photos_uploaded_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.images.model import Image, ImageSource

    return _grouped_count(Image.objects.filter(profile_id__in=profile_ids, source=ImageSource.UPLOAD), "profile_id")


def _places_visited(profile: Profile) -> int:
    from urbanlens.dashboard.models.visits.model import PinVisit

    # Distinct pins, not visit rows - going back to the same place ten times is
    # one place visited.
    return PinVisit.objects.filter(pin__profile=profile, tentative=False).values("pin_id").distinct().count()


def _places_visited_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.visits.model import PinVisit

    return _grouped_count(
        PinVisit.objects.filter(pin__profile_id__in=profile_ids, tentative=False),
        "pin__profile_id",
        count_field="pin_id",
        distinct=True,
    )


def _places_rated(profile: Profile) -> int:
    from urbanlens.dashboard.models.reviews.model import Review

    return Review.objects.filter(profile=profile).count()


def _places_rated_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.reviews.model import Review

    return _grouped_count(Review.objects.filter(profile_id__in=profile_ids), "profile_id")


def _places_vulnerability_rated(profile: Profile) -> int:
    from urbanlens.dashboard.models.pin.model import Pin

    # vulnerability/danger default to 0 and live on the pin itself, so "rated"
    # means the user moved it off the default.
    return Pin.objects.filter(profile=profile, vulnerability__gt=0).count()


def _places_vulnerability_rated_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.pin.model import Pin

    return _grouped_count(Pin.objects.filter(profile_id__in=profile_ids, vulnerability__gt=0), "profile_id")


def _places_danger_rated(profile: Profile) -> int:
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.filter(profile=profile, danger__gt=0).count()


def _places_danger_rated_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.pin.model import Pin

    return _grouped_count(Pin.objects.filter(profile_id__in=profile_ids, danger__gt=0), "profile_id")


def _friends(profile: Profile) -> int:
    from urbanlens.dashboard.models.friendship.model import Friendship

    # One shared row joins each pair, so this does not double-count.
    return Friendship.objects.profile(profile).is_friend().count()


def _friends_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.friendship.model import Friendship

    # A profile can sit on either end of the pair's single shared row, so this
    # groups each side separately and sums. A row joining two profiles in the
    # same chunk credits both, exactly as the per-profile count does; the
    # self-referencing exclusion mirrors ``Q(from=p) | Q(to=p)`` counting such
    # a row once, not twice.
    accepted = Friendship.objects.is_friend()
    counts = _grouped_count(accepted.filter(from_profile_id__in=profile_ids), "from_profile_id")
    to_side = accepted.filter(to_profile_id__in=profile_ids).exclude(from_profile=F("to_profile"))
    for pk, count in _grouped_count(to_side, "to_profile_id").items():
        counts[pk] = counts.get(pk, 0) + count
    return counts


def _trips_planned(profile: Profile) -> int:
    from urbanlens.dashboard.models.trips.model import Trip

    return Trip.objects.filter(creator=profile).count()


def _trips_planned_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.trips.model import Trip

    return _grouped_count(Trip.objects.filter(creator_id__in=profile_ids), "creator_id")


def _finished_trip_q(today: datetime.date) -> Q:
    """Match trip memberships whose trip has ended as of *today*.

    end_date is optional, so a trip with none falls back to its start_date.
    """
    return Q(trip__end_date__lt=today) | Q(trip__end_date__isnull=True, trip__start_date__lt=today)


def _trips_attended(profile: Profile) -> int:
    from urbanlens.dashboard.models.trips.model import TripMembership

    # A trip counts as attended once it is over and the member had joined
    # without declining.
    return TripMembership.objects.filter(_finished_trip_q(timezone.localdate()), profile=profile, status=TripMembership.STATUS_JOINED).exclude(rsvp=TripMembership.RSVP_NO).count()


def _trips_attended_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.trips.model import TripMembership

    memberships = TripMembership.objects.filter(
        _finished_trip_q(timezone.localdate()),
        profile_id__in=profile_ids,
        status=TripMembership.STATUS_JOINED,
    ).exclude(rsvp=TripMembership.RSVP_NO)
    return _grouped_count(memberships, "profile_id")


def _wiki_edits(profile: Profile) -> int:
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

    return WikiEdit.objects.filter(editor=profile, reverted=False).count()


def _wiki_edits_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

    return _grouped_count(WikiEdit.objects.filter(editor_id__in=profile_ids, reverted=False), "editor_id")


def _comments_written(profile: Profile) -> int:
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    return Comment.objects.filter(profile=profile).count() + TripComment.objects.filter(author=profile).count()


def _comments_written_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    counts = _grouped_count(Comment.objects.filter(profile_id__in=profile_ids), "profile_id")
    for pk, count in _grouped_count(TripComment.objects.filter(author_id__in=profile_ids), "author_id").items():
        counts[pk] = counts.get(pk, 0) + count
    return counts


def _markup_maps_created(profile: Profile) -> int:
    from urbanlens.dashboard.models.markup.model import MarkupMap

    return MarkupMap.objects.filter(profile=profile).count()


def _markup_maps_created_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.markup.model import MarkupMap

    return _grouped_count(MarkupMap.objects.filter(profile_id__in=profile_ids), "profile_id")


def _people_invited(profile: Profile) -> int:
    from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation

    # Accepted invitations only - sending mail to an address nobody signs up
    # from is not a contribution, and rewarding it invites address harvesting.
    return FriendInvitation.objects.filter(inviter=profile, accepted_at__isnull=False).count()


def _people_invited_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
    from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation

    return _grouped_count(FriendInvitation.objects.filter(inviter_id__in=profile_ids, accepted_at__isnull=False), "inviter_id")


def _longest_streak(kind: str) -> Callable[[Profile], int]:
    """Return a compute function reading the cached longest streak for *kind*."""

    def compute(profile: Profile) -> int:
        from urbanlens.dashboard.models.achievements.model import ProfileStreak

        row = ProfileStreak.objects.filter(profile=profile, kind=kind).values_list("longest_length", flat=True).first()
        return row or 0

    return compute


def _longest_streak_bulk(kind: str) -> Callable[[Sequence[int]], dict[int, int]]:
    """Return a bulk compute function reading cached longest streaks for *kind*.

    Streak arithmetic is path-dependent, but this reads none of it: the
    incremental tracker already collapsed the history into
    ``ProfileStreak.longest_length``, so the bulk form is a plain grouped read
    of that column - exactly what the per-profile form does, minus N queries.
    """

    def compute_bulk(profile_ids: Sequence[int]) -> dict[int, int]:
        from urbanlens.dashboard.models.achievements.model import ProfileStreak

        rows = ProfileStreak.objects.filter(profile_id__in=profile_ids, kind=kind).values_list("profile_id", "longest_length")
        return dict(rows)

    return compute_bulk


_STREAK_LABELS: dict[str, tuple[str, str]] = {
    ActivityKind.LOGIN: ("Login streak", "Log in on {threshold} days in a row"),
    ActivityKind.PHOTO: ("Photo streak", "Upload a photo on {threshold} days in a row"),
    ActivityKind.WIKI_EDIT: ("Wiki edit streak", "Edit a wiki on {threshold} days in a row"),
    ActivityKind.PIN: ("Pinning streak", "Pin a spot on {threshold} days in a row"),
    ActivityKind.COMMENT: ("Comment streak", "Leave a comment on {threshold} days in a row"),
}


def _register_builtin_metrics() -> None:
    """Register the metrics that ship with the app.

    Called at import time. Idempotent, so a re-import cannot duplicate entries.
    """
    for metric in (
        Metric(
            key="pins_created",
            label="Pins created",
            unit="pins",
            description="Pins the user has saved, including ones nested under another pin.",
            compute=_pins_created,
            compute_bulk=_pins_created_bulk,
            group=GROUP_CONTENT,
            triggers=frozenset({TRIGGER_PIN}),
            requirement_template="Save {threshold} pins",
        ),
        Metric(
            key="wiki_edits",
            label="Wiki edits",
            unit="edits",
            description="Edits the user made to community wikis, excluding edits that were reverted.",
            compute=_wiki_edits,
            compute_bulk=_wiki_edits_bulk,
            group=GROUP_CONTENT,
            triggers=frozenset({TRIGGER_WIKI_EDIT}),
            requirement_template="Make {threshold} wiki edits",
        ),
        Metric(
            key="photos_uploaded",
            label="Photos uploaded",
            unit="photos",
            description="Photos the user uploaded themselves, excluding photos imported from external providers.",
            compute=_photos_uploaded,
            compute_bulk=_photos_uploaded_bulk,
            group=GROUP_CONTENT,
            triggers=frozenset({TRIGGER_PHOTO}),
            requirement_template="Upload {threshold} photos",
        ),
        Metric(
            key="markup_maps_created",
            label="Markup maps created",
            unit="maps",
            description="Standalone annotated maps the user has drawn.",
            compute=_markup_maps_created,
            compute_bulk=_markup_maps_created_bulk,
            group=GROUP_CONTENT,
            triggers=frozenset({TRIGGER_MARKUP_MAP}),
            requirement_template="Draw {threshold} markup maps",
        ),
        Metric(
            key="places_visited",
            label="Places visited",
            unit="places",
            description="Distinct pinned places with a confirmed visit logged. Repeat visits count once.",
            compute=_places_visited,
            compute_bulk=_places_visited_bulk,
            group=GROUP_EXPLORATION,
            triggers=frozenset({TRIGGER_VISIT}),
            requirement_template="Visit {threshold} places",
        ),
        Metric(
            key="places_rated",
            label="Places rated (stars)",
            unit="places",
            description="Places the user gave a star rating.",
            compute=_places_rated,
            compute_bulk=_places_rated_bulk,
            group=GROUP_EXPLORATION,
            triggers=frozenset({TRIGGER_REVIEW}),
            requirement_template="Rate {threshold} places",
        ),
        Metric(
            key="places_vulnerability_rated",
            label="Places rated (vulnerability)",
            unit="places",
            description="Pins the user assigned a non-zero vulnerability rating.",
            compute=_places_vulnerability_rated,
            compute_bulk=_places_vulnerability_rated_bulk,
            group=GROUP_EXPLORATION,
            triggers=frozenset({TRIGGER_PIN}),
            requirement_template="Rate the vulnerability of {threshold} places",
        ),
        Metric(
            key="places_danger_rated",
            label="Places rated (danger)",
            unit="places",
            description="Pins the user assigned a non-zero danger rating.",
            compute=_places_danger_rated,
            compute_bulk=_places_danger_rated_bulk,
            group=GROUP_EXPLORATION,
            triggers=frozenset({TRIGGER_PIN}),
            requirement_template="Rate the danger of {threshold} places",
        ),
        Metric(
            key="trips_planned",
            label="Trips planned",
            unit="trips",
            description="Trips the user created.",
            compute=_trips_planned,
            compute_bulk=_trips_planned_bulk,
            group=GROUP_EXPLORATION,
            triggers=frozenset({TRIGGER_TRIP}),
            requirement_template="Plan {threshold} trips",
        ),
        Metric(
            key="trips_attended",
            label="Trips attended",
            unit="trips",
            description="Finished trips the user had joined and had not declined.",
            compute=_trips_attended,
            compute_bulk=_trips_attended_bulk,
            group=GROUP_EXPLORATION,
            triggers=frozenset({TRIGGER_TRIP, TRIGGER_TRIP_MEMBERSHIP}),
            requirement_template="Attend {threshold} trips",
        ),
        Metric(
            key="comments_written",
            label="Comments written",
            unit="comments",
            description="Comments on pins, wikis and trips.",
            compute=_comments_written,
            compute_bulk=_comments_written_bulk,
            group=GROUP_COMMUNITY,
            triggers=frozenset({TRIGGER_COMMENT}),
            requirement_template="Write {threshold} comments",
        ),
        Metric(
            key="friends",
            label="Friends",
            unit="friends",
            description="Accepted friendships, in either direction.",
            compute=_friends,
            compute_bulk=_friends_bulk,
            group=GROUP_COMMUNITY,
            triggers=frozenset({TRIGGER_FRIENDSHIP}),
            requirement_template="Make {threshold} friends",
        ),
        Metric(
            key="people_invited",
            label="People invited to the site",
            unit="people",
            description="Invitations the user sent that were accepted and became accounts.",
            compute=_people_invited,
            compute_bulk=_people_invited_bulk,
            group=GROUP_COMMUNITY,
            triggers=frozenset({TRIGGER_INVITATION}),
            requirement_template="Invite {threshold} people who join",
        ),
    ):
        register(metric)

    for kind, (label, requirement) in _STREAK_LABELS.items():
        register(
            Metric(
                key=streak_metric_key(kind),
                label=label,
                unit="days",
                description=f"Longest run of consecutive days on which the user performed: {ActivityKind(kind).label.lower()}.",
                compute=_longest_streak(kind),
                compute_bulk=_longest_streak_bulk(kind),
                group=GROUP_STREAKS,
                triggers=frozenset({TRIGGER_STREAK}),
                requirement_template=requirement,
            ),
        )


_register_builtin_metrics()


def compute_values(profile: Profile, keys: Iterable[str] | None = None) -> dict[str, int]:
    """Return current values for *keys* (or every metric) for one profile.

    Args:
        profile: The profile to measure.
        keys: Metric keys to compute; None means all registered metrics.

    Returns:
        A mapping of metric key to current value. Unknown keys are skipped.
    """
    selected = all_metrics() if keys is None else [m for k in dict.fromkeys(keys) if (m := get_metric(k))]
    return {metric.key: metric.value_for(profile) for metric in selected}


def compute_values_bulk(profiles: Sequence[Profile], keys: Iterable[str] | None = None) -> dict[int, dict[str, int]]:
    """Return current values for *keys* (or every metric) for many profiles.

    The bulk counterpart of :func:`compute_values`, used by the nightly sweep:
    each metric that defines ``compute_bulk`` is computed for the whole batch
    in a constant number of grouped queries, so the batch costs on the order
    of the metric count in queries rather than metrics x profiles. Metrics
    without a bulk form fall back to per-profile computation.

    Args:
        profiles: The profiles to measure.
        keys: Metric keys to compute; None means all registered metrics.

    Returns:
        A mapping of profile pk to that profile's ``{metric key: value}``
        mapping, exactly as :func:`compute_values` would have returned for it.
        Unknown keys are skipped.
    """
    selected = all_metrics() if keys is None else [m for k in dict.fromkeys(keys) if (m := get_metric(k))]
    values: dict[int, dict[str, int]] = {profile.pk: {} for profile in profiles}
    for metric in selected:
        for pk, value in metric.values_for_many(profiles).items():
            values[pk][metric.key] = value
    return values


def streak_summary(profile: Profile, today: datetime.date | None = None) -> list[StreakSummaryRow]:
    """Return per-kind streak state for a profile, for display on the profile page.

    Args:
        profile: The profile to summarise.
        today: Date to judge "still running" against; defaults to the local date.

    Returns:
        One dict per :class:`ActivityKind` with ``kind``, ``label``, ``current``
        and ``longest`` keys, ordered as declared on the enum.
    """
    from urbanlens.dashboard.models.achievements.model import ProfileStreak

    today = today or timezone.localdate()
    rows = {row.kind: row for row in ProfileStreak.objects.for_profile(profile)}
    summary: list[StreakSummaryRow] = []
    for kind in ActivityKind:
        row = rows.get(kind.value)
        summary.append(
            {
                "kind": kind.value,
                "label": kind.label,
                "current": row.current_length_as_of(today) if row else 0,
                "longest": row.longest_length if row else 0,
            },
        )
    return summary
