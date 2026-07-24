"""AI trip suggestions (UL-60): pins worth adding, and a drive/weather/vote-aware schedule.

Privacy model - two independent gates, both required before anything reaches the model:

1. CANDIDATE POOL. A location is only ever offered as an "add this pin"
   suggestion when EVERY joined trip member already has it pinned on their
   own map, unconditionally - this never bypasses even for members who have
   external sharing off, because it's a pure membership check, never data
   sent anywhere. Suggesting a location only some members have would leak
   that member's private pin to the others; requiring universal membership
   makes that structurally impossible.
2. PERSONAL SIGNALS. For a candidate location, only members with
   ``Profile.external_apis_enabled`` on contribute their own visited /
   priority / vulnerability / danger signal to what the model sees. Members
   who opted out still count toward "does everyone have this pinned" (that
   check never touches the model) but their personal ratings are withheld
   completely - not aggregated, not anonymized-and-included, just absent.

Existing trip activities (title, schedule, aggregate up/down votes) are
already visible to every joined member through the trip page itself, so they
need no extra gating beyond the same per-viewer location-visibility rule the
trip page already applies (``services.trip_visibility``) - an activity whose
location is hidden from the requester is dropped before it ever reaches the
prompt, exactly as it would be dropped from their view of the page itself.

No member's identity (name, username, profile id) is ever sent to the model.
Per-member signals are labeled "Member 1", "Member 2"... using a dense,
trip-scoped anonymous index assigned only across sharing-enabled members (so
the numbering itself can't reveal that non-participating members exist).

Results are cached per (trip, requester) for a short TTL - the underlying
data is shared/anonymized already, but each viewer's own pin slugs (needed
for their personal "Add to trip" button) differ, so caching stays per-viewer
rather than trying to share one cache entry across the whole trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.core.cache import cache

from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.json_answer import parse_json_answer
from urbanlens.dashboard.services.trip_legs import TripLeg, activity_coords, compute_legs
from urbanlens.dashboard.services.trip_visibility import viewer_hidden_activity_ids

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip, TripActivity

logger = logging.getLogger(__name__)

#: Candidate pins and existing activities sent to the model, each capped -
#: bounds prompt size regardless of how large the trip or its members' maps are.
MAX_CANDIDATES = 20
MAX_ACTIVITIES_IN_PROMPT = 40
#: How long a generated result is served from cache before a fresh call is made.
CACHE_TTL_SECONDS = 15 * 60
#: Minimum gap between two "Refresh" (force) generations for the same viewer+trip.
REFRESH_COOLDOWN_SECONDS = 60


@dataclass(slots=True, frozen=True)
class MemberSignal:
    """One anonymized trip member's personal read on a candidate pin."""

    member_label: str
    visited: bool
    priority: int | None
    vulnerability: int | None
    danger: int | None


@dataclass(slots=True, frozen=True)
class CandidatePin:
    """A location every joined member already has pinned - a possible trip addition."""

    location_id: int
    name: str
    add_pin_slug: str
    signals: list[MemberSignal]


@dataclass(slots=True, frozen=True)
class ExistingActivity:
    """One of the trip's current (non-completed) activities, as the requester may see it."""

    activity_id: int
    title: str
    status: str
    scheduled_at: datetime.datetime | None
    votes_up: int
    votes_down: int


@dataclass(slots=True, frozen=True)
class TripAiContext:
    """Everything privacy-cleared to send to the model for one (trip, requester) pair."""

    trip_name: str
    start_date: datetime.date | None
    end_date: datetime.date | None
    duration_days: int | None
    participant_count: int
    candidates: list[CandidatePin] = field(default_factory=list)
    activities: list[ExistingActivity] = field(default_factory=list)
    legs: dict[int, TripLeg] = field(default_factory=dict)
    weather_days: list[dict] = field(default_factory=list)
    weather_note: str = ""


@dataclass(slots=True, frozen=True)
class PinSuggestion:
    """One AI-suggested pin to add, resolved back to the viewer's own data."""

    location_id: int
    name: str
    add_pin_slug: str
    reason: str


@dataclass(slots=True, frozen=True)
class ScheduleSuggestion:
    """A suggested re-ordering of the trip's existing activities."""

    ordered_activity_ids: list[int]
    reason: str


@dataclass(slots=True, frozen=True)
class TripSuggestions:
    """The full result of one suggestion generation, ready to render."""

    summary: str
    pin_suggestions: list[PinSuggestion] = field(default_factory=list)
    schedule: ScheduleSuggestion | None = None
    generated: bool = True


_UNAVAILABLE = TripSuggestions(summary="", pin_suggestions=[], schedule=None, generated=False)


def _joined_profiles(trip: Trip) -> list[Profile]:
    """Profiles of every member who has actually joined the trip (not just invited).

    The creator is always treated as joined, matching ``_viewer_has_joined``/
    ``_can_perform`` elsewhere - defensive against a creator row somehow
    missing its own membership, since this set gates what's privacy-safe to
    show the model and under-counting it would be the safe direction anyway,
    but over-counting (treating a non-member as joined) never happens here.
    """
    from urbanlens.dashboard.models.trips.model import TripMembership

    profiles = {m.profile_id: m.profile for m in TripMembership.objects.filter(trip=trip, status=TripMembership.STATUS_JOINED).select_related("profile")}
    if trip.creator_id is not None and trip.creator_id not in profiles:
        profiles[trip.creator_id] = trip.creator
    return list(profiles.values())


def _common_location_ids(profiles: list[Profile]) -> set[int]:
    """Location ids where every given profile has a root pin - the anti-leak candidate gate."""
    from urbanlens.dashboard.models.pin.model import Pin

    if not profiles:
        return set()
    location_sets = [set(Pin.objects.filter(profile=profile, parent_pin__isnull=True, location__isnull=False).values_list("location_id", flat=True)) for profile in profiles]
    return set.intersection(*location_sets)


def _build_candidates(profiles: list[Profile], requester: Profile, exclude_location_ids: set[int]) -> list[CandidatePin]:
    """Common, not-yet-added locations with per-member signals from sharing-enabled members only."""
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.visits.model import PinVisit

    common_ids = _common_location_ids(profiles) - exclude_location_ids
    if not common_ids:
        return []

    sharing_profiles = [profile for profile in profiles if profile.external_apis_enabled]
    label_by_profile_id = {profile.id: f"Member {index + 1}" for index, profile in enumerate(sorted(sharing_profiles, key=lambda profile: profile.id))}

    requester_pins = {pin.location_id: pin for pin in Pin.objects.filter(profile=requester, location_id__in=common_ids, parent_pin__isnull=True)}
    sharing_ids = [profile.id for profile in sharing_profiles]
    pins: list[Pin] = list(Pin.objects.filter(location_id__in=common_ids, profile_id__in=sharing_ids, parent_pin__isnull=True).only("id", "location_id", "profile_id", "priority", "vulnerability", "danger"))
    visited_pin_ids = set(PinVisit.objects.filter(pin_id__in=[pin.id for pin in pins]).values_list("pin_id", flat=True))

    by_location: dict[int, list[Pin]] = {}
    for pin in pins:
        by_location.setdefault(pin.location_id, []).append(pin)

    candidates: list[CandidatePin] = []
    for location_id in common_ids:
        requester_pin = requester_pins.get(location_id)
        location_pins = by_location.get(location_id)
        if requester_pin is None or not location_pins:
            # Requester must have their own pin to add it (guaranteed by the
            # intersection above); no sharing-enabled data means nothing safe
            # to tell the model about this location, so it's skipped rather
            # than suggested blind.
            continue
        signals = [
            MemberSignal(
                member_label=label_by_profile_id[pin.profile_id],
                visited=pin.id in visited_pin_ids,
                priority=pin.priority or None,
                vulnerability=pin.vulnerability or None,
                danger=pin.danger or None,
            )
            for pin in sorted(location_pins, key=lambda pin: label_by_profile_id[pin.profile_id])
        ]
        candidates.append(CandidatePin(location_id=location_id, name=requester_pin.effective_name, add_pin_slug=requester_pin.slug, signals=signals))

    def _score(candidate: CandidatePin) -> tuple[int, float]:
        unvisited = sum(1 for signal in candidate.signals if not signal.visited)
        priorities = [signal.priority for signal in candidate.signals if signal.priority]
        avg_priority = sum(priorities) / len(priorities) if priorities else 0.0
        return (unvisited, avg_priority)

    candidates.sort(key=_score, reverse=True)
    return candidates[:MAX_CANDIDATES]


def _visible_activities(trip: Trip, viewer: Profile) -> list[TripActivity]:
    """The trip's non-completed activities, dropping anything this viewer can't see."""
    from django.db.models import F

    from urbanlens.dashboard.models.trips.model import TripActivity

    activities = list(trip.activities.select_related("location", "pin", "pin__location", "added_by__user").exclude(status=TripActivity.STATUS_COMPLETED).order_by(F("scheduled_at").asc(nulls_last=True), "order", "created"))
    hidden = viewer_hidden_activity_ids(activities, viewer)
    return [activity for activity in activities if activity.id not in hidden][:MAX_ACTIVITIES_IN_PROMPT]


def _vote_counts(activity_ids: list[int]) -> dict[int, tuple[int, int]]:
    """activity_id -> (up, down); aggregate counts only, matching what the trip page already shows."""
    from urbanlens.dashboard.models.trips.model import TripActivityVote

    counts: dict[int, tuple[int, int]] = dict.fromkeys(activity_ids, (0, 0))
    for activity_id, vote in TripActivityVote.objects.filter(activity_id__in=activity_ids).values_list("activity_id", "vote"):
        up, down = counts[activity_id]
        counts[activity_id] = (up + 1, down) if vote == "up" else (up, down + 1)
    return counts


def _weather_summary(activities: list[TripActivity], requester: Profile) -> tuple[list[dict], str]:
    """Daily condition/temperature summary for the trip's date range, or an explanation why not."""
    import requests as requests_lib

    from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not requester.external_apis_enabled:
        return [], "Weather skipped - external lookups are off in your settings."
    if not app_settings.openweathermap_api_key:
        return [], "Weather unavailable - no provider configured."

    # (activity, scheduled_at) pairs keep scheduled_at's non-None type intact
    # for every use below, rather than re-narrowing `activity.scheduled_at`
    # (Optional) again at each site.
    dated = [(activity, activity.scheduled_at) for activity in activities if activity.scheduled_at is not None]
    if not dated:
        return [], "No scheduled activities yet to fetch weather for."
    first, _first_at = min(dated, key=lambda pair: pair[1])
    coords = activity_coords(first)
    if coords is None:
        return [], "No activity with a known location to fetch weather for."

    try:
        gateway = OpenWeatherMapGateway()
        slots = gateway.get_raw_forecast(*coords)
    except (requests_lib.RequestException, ValueError):
        logger.warning("Weather lookup failed for trip AI suggestions", exc_info=True)
        return [], "Weather lookup failed."
    if not slots:
        return [], "No forecast data returned."

    start = min(scheduled_at.date() for _activity, scheduled_at in dated)
    end = max(scheduled_at.date() for _activity, scheduled_at in dated)
    by_day: dict[datetime.date, list[dict]] = {}
    for slot in slots:
        slot_date = slot.get("date")
        if slot_date is None:
            continue
        day = slot_date.date()
        if start <= day <= end:
            by_day.setdefault(day, []).append(slot)

    if not by_day:
        return [], "Trip dates are outside the 5-day forecast window."

    days = []
    for day in sorted(by_day):
        day_slots = by_day[day]
        temps = [slot["main"]["temp"] for slot in day_slots if slot.get("main", {}).get("temp") is not None]
        conditions = [slot["weather"][0]["main"] for slot in day_slots if slot.get("weather")]
        condition = max(set(conditions), key=conditions.count) if conditions else "Unknown"
        days.append({"date": day.isoformat(), "condition": condition, "low": round(min(temps)) if temps else None, "high": round(max(temps)) if temps else None})
    return days, ""


def build_trip_context(trip: Trip, requester: Profile) -> TripAiContext:
    """Assemble the full privacy-cleared context for one (trip, requester) pair."""
    profiles = _joined_profiles(trip)
    existing_location_ids = set(trip.activities.filter(location__isnull=False).values_list("location_id", flat=True))
    candidates = _build_candidates(profiles, requester, existing_location_ids)

    visible_activities = _visible_activities(trip, requester)
    leg_stops = [(activity.id, coords) for activity in visible_activities if (coords := activity_coords(activity)) is not None]
    legs = compute_legs(leg_stops) if len(leg_stops) >= 2 else {}
    votes = _vote_counts([activity.id for activity in visible_activities])
    activities = [
        ExistingActivity(
            activity_id=activity.id,
            title=activity.effective_title,
            status=activity.status,
            scheduled_at=activity.scheduled_at,
            votes_up=votes.get(activity.id, (0, 0))[0],
            votes_down=votes.get(activity.id, (0, 0))[1],
        )
        for activity in visible_activities
    ]
    weather_days, weather_note = _weather_summary(visible_activities, requester)

    return TripAiContext(
        trip_name=trip.name or "Untitled trip",
        start_date=trip.effective_start_date,
        end_date=trip.effective_end_date,
        duration_days=trip.duration_days,
        participant_count=len(profiles),
        candidates=candidates,
        activities=activities,
        legs=legs,
        weather_days=weather_days,
        weather_note=weather_note,
    )


_INSTRUCTIONS = (
    "You are UrbanLens's trip-planning assistant. Everything below has already been "
    "privacy-filtered for you - never assume any information beyond what's explicitly "
    "listed, and never refer to members by anything but their given label (e.g. 'Member 2'). "
    'Respond with EXACTLY ONE JSON object and nothing else: {"summary": str, '
    '"pin_suggestions": [{"index": int, "reason": str}], "schedule": {"order": [int, ...], '
    '"reason": str}}. "index" must be one of the numbered candidate pins given (omit '
    'pin_suggestions or leave it empty if none are worth adding). "order" must be a '
    'permutation of the existing activity ids given, in your suggested order (omit '
    '"schedule" or set it to null if no reordering is useful, e.g. fewer than two '
    "activities, or the current order is already good). Keep every reason to one short, "
    "concrete sentence grounded in the data given - never invent places, ratings, dates, or "
    "people not listed above."
)
_FORMATTING = "Wrap the JSON object in <ANSWER></ANSWER> tags with nothing else inside them."


def _format_signal(signal: MemberSignal) -> str:
    bits = [f"visited={'yes' if signal.visited else 'no'}"]
    if signal.priority:
        bits.append(f"priority={signal.priority}/5")
    if signal.vulnerability:
        bits.append(f"vulnerability={signal.vulnerability}/5")
    if signal.danger:
        bits.append(f"danger={signal.danger}/5")
    return f"{signal.member_label}: {', '.join(bits)}"


def _format_prompt(context: TripAiContext) -> str:
    lines = [f'Trip: "{context.trip_name}"']
    if context.start_date and context.end_date:
        lines.append(f"Dates: {context.start_date.isoformat()} to {context.end_date.isoformat()} ({context.duration_days} days)")
    elif context.start_date:
        lines.append(f"Starts: {context.start_date.isoformat()} (no end date set)")
    else:
        lines.append("Dates: not set yet")
    lines.append(f"Participants (joined members): {context.participant_count}")

    if context.weather_days:
        lines.append("Weather forecast:")
        for day in context.weather_days:
            temp = f", {day['low']}-{day['high']}F" if day["low"] is not None else ""
            lines.append(f"  - {day['date']}: {day['condition']}{temp}")
    elif context.weather_note:
        lines.append(f"Weather: {context.weather_note}")

    lines.append("")
    lines.append("Existing itinerary (these already exist - suggest a better order if useful, never suggest them again as new pins):")
    if context.activities:
        for activity in context.activities:
            when = activity.scheduled_at.strftime("%Y-%m-%d %H:%M") if activity.scheduled_at else "unscheduled"
            leg = context.legs.get(activity.activity_id)
            leg_text = f", {leg.duration_display} drive from the previous stop" if leg else ""
            lines.append(f'  - id={activity.activity_id} "{activity.title}" [{activity.status}] scheduled={when}{leg_text}, votes: +{activity.votes_up}/-{activity.votes_down}')
    else:
        lines.append("  (none yet)")

    lines.append("")
    lines.append("Candidate pins every participant already has on their own map (never suggest a location not listed here):")
    if context.candidates:
        for index, candidate in enumerate(context.candidates, start=1):
            lines.append(f'  {index}. "{candidate.name}"')
            for signal in candidate.signals:
                lines.append(f"     {_format_signal(signal)}")
    else:
        lines.append("  (none - no single location is pinned by every participant yet)")

    lines.append("")
    lines.append(
        "Suggest up to 5 of the numbered candidates worth adding (favor ones most "
        "participants haven't visited and with a reasonable priority/danger balance for "
        "the group), and optionally a better order for the existing itinerary factoring in "
        "drive time, votes, and weather.",
    )
    return "\n".join(lines)


def _resolve_pin_suggestions(parsed: dict, context: TripAiContext) -> list[PinSuggestion]:
    raw_items = parsed.get("pin_suggestions")
    if not isinstance(raw_items, list):
        return []
    resolved: list[PinSuggestion] = []
    seen_indices: set[int] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index")
        if raw_index is None:
            continue
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if index in seen_indices or not (1 <= index <= len(context.candidates)):
            continue
        seen_indices.add(index)
        candidate = context.candidates[index - 1]
        reason = str(item.get("reason") or "").strip()[:300]
        resolved.append(PinSuggestion(location_id=candidate.location_id, name=candidate.name, add_pin_slug=candidate.add_pin_slug, reason=reason))
    return resolved[:5]


def _resolve_schedule(parsed: dict, context: TripAiContext) -> ScheduleSuggestion | None:
    raw_schedule = parsed.get("schedule")
    if not isinstance(raw_schedule, dict):
        return None
    raw_order = raw_schedule.get("order")
    if not isinstance(raw_order, list):
        return None
    try:
        order = [int(value) for value in raw_order]
    except (TypeError, ValueError):
        return None
    valid_ids = {activity.activity_id for activity in context.activities}
    # Only ever accept a genuine reordering: an exact permutation of the
    # activities we offered, no more, no fewer, no duplicates, nothing
    # hallucinated - anything else is silently dropped rather than applied.
    if len(order) != len(valid_ids) or set(order) != valid_ids or not valid_ids:
        return None
    reason = str(raw_schedule.get("reason") or "").strip()[:300]
    return ScheduleSuggestion(ordered_activity_ids=order, reason=reason)


def generate_trip_suggestions(trip: Trip, requester: Profile) -> TripSuggestions:
    """Generate fresh AI trip suggestions - always live, never cached (see get_trip_suggestions).

    Returns:
        A ``TripSuggestions`` with ``generated=False`` when AI is unavailable
        for this profile/site, otherwise the (possibly empty) result.
    """
    gateway = get_gateway(profile=requester, feature="trip_suggestions", instructions=_INSTRUCTIONS, formatting=_FORMATTING)
    if gateway is None:
        return _UNAVAILABLE

    context = build_trip_context(trip, requester)
    answer = gateway.send_prompt(_format_prompt(context))
    if not answer:
        return TripSuggestions(summary="Couldn't reach the AI provider just now - try again shortly.")

    parsed = parse_json_answer(answer)
    if parsed is None:
        return TripSuggestions(summary="The assistant's response couldn't be understood - try refreshing.")

    summary = str(parsed.get("summary") or "").strip()[:1000]
    return TripSuggestions(summary=summary, pin_suggestions=_resolve_pin_suggestions(parsed, context), schedule=_resolve_schedule(parsed, context))


def _cache_key(trip: Trip, requester: Profile) -> str:
    return f"dashboard:trip_ai_suggestions:{trip.pk}:{requester.pk}"


def _cooldown_key(trip: Trip, requester: Profile) -> str:
    return f"dashboard:trip_ai_suggestions:cooldown:{trip.pk}:{requester.pk}"


def get_trip_suggestions(trip: Trip, requester: Profile, *, force_refresh: bool = False) -> TripSuggestions:
    """Cached entry point the trip page actually calls.

    Args:
        trip: The trip to generate suggestions for.
        requester: The joined member asking - gates AI availability and
            resolves their own "Add to trip" pin slugs.
        force_refresh: Bypass the cache, subject to a short per-viewer
            cooldown (serves the last cached result instead of erroring, or
            generates fresh with nothing cached yet).

    Returns:
        The (possibly cached) suggestions. ``generated=False`` means AI is
        unavailable for this profile/site - render an explanatory state, not
        an empty suggestions list.
    """
    key = _cache_key(trip, requester)
    if not force_refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    if force_refresh:
        cooldown_key = _cooldown_key(trip, requester)
        if cache.get(cooldown_key):
            return cache.get(key) or _UNAVAILABLE
        cache.set(cooldown_key, 1, REFRESH_COOLDOWN_SECONDS)

    result = generate_trip_suggestions(trip, requester)
    if result.generated:
        cache.set(key, result, CACHE_TTL_SECONDS)
    return result
