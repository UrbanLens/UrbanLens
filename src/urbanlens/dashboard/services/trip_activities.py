"""Trip activity behavior, shared by the internal HTMX panel and the external REST API.

Everything an itinerary entry can do lives here: building the per-activity
render rows (index, votes, RSVP, permissions, visibility, driving legs) and
every mutation (create, update, delete, reposition, vote, status, RSVP,
complete, reorder).

Cross-cutting obligations that must never be re-implemented by a caller are
enforced inside these functions rather than beside them:

- share provenance (``trip_share_tracking.record_trip_activity_shares``) on create,
- ``SiteSettings.max_trip_activities`` quota on create,
- text limits checked *before* ``save()`` so an over-long note is a 400 rather
  than an uncaught ``ValidationError``,
- per-viewer location visibility (``trip_visibility.viewer_hidden_activity_ids``),
- identity masking for the "added by" attribution.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from django.db.models import F
from django.utils import timezone

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import (
    Trip,
    TripActivity,
    TripActivityRSVP,
    TripActivityVote,
    TripMembership,
)
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.text_limits import MAX_TRIP_ACTIVITY_NOTES_LENGTH, text_length_error
from urbanlens.dashboard.services.trip_access import has_joined, is_organizer, require_joined, require_perform
from urbanlens.dashboard.services.trip_errors import TripNotFoundError, TripQuotaError, TripValidationError
from urbanlens.dashboard.services.trip_legs import activity_coords
from urbanlens.dashboard.services.trip_visibility import viewer_hidden_activity_ids
from urbanlens.dashboard.services.visits import add_visited_status, create_visit_suggestion, get_or_create_pin_at, sync_last_visited, visit_logging_allowed

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

#: The two statuses a caller may set directly. ``completed`` is reached only
#: through :func:`complete_activity`, which also logs visits and snaps dates.
SETTABLE_STATUSES = frozenset({TripActivity.STATUS_PROPOSED, TripActivity.STATUS_CONFIRMED})

#: Truthy spellings accepted for boolean fields arriving as form/JSON text.
_TRUTHY = frozenset({"true", "1", "on", "yes"})

# -- refusal messages (kept as constants so both surfaces stay in step) --------

ADD_ACTIVITY_DENIED = "You don't have permission to add activities to this trip."
EDIT_ACTIVITY_DENIED = "You don't have permission to edit activities on this trip."
DELETE_ACTIVITY_DENIED = "You don't have permission to delete activities on this trip."
REORDER_ACTIVITY_DENIED = "You don't have permission to reorder activities."
MOVE_ACTIVITY_DENIED = "You don't have permission to move activities on this trip."
CONTRIBUTE_DENIED = "Join this trip to contribute."
ACTIVITY_RSVP_DENIED = "Join this trip before responding to activities."
ACTIVITY_NOT_FOUND = "No such activity."


def _as_bool(value: Any) -> bool:
    """Coerce a form/JSON value to a bool, accepting the usual text spellings.

    Args:
        value: A real bool, or one of the ``"true"``/``"1"``/``"on"`` spellings
            an HTML form or a loosely-typed JSON body produces.

    Returns:
        The boolean interpretation; anything unrecognized is False.
    """
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def _clean_text(value: Any) -> str | None:
    """Strip a free-text field, collapsing blank/absent to ``None``.

    Args:
        value: The raw submitted value.

    Returns:
        The stripped text, or None when empty/absent.
    """
    if value is None:
        return None
    return str(value).strip() or None


def activity_queryset(trip: Trip) -> QuerySet[TripActivity]:
    """Return the trip's activities in itinerary order with every relation needed to render them.

    Args:
        trip: The trip whose activities are wanted.

    Returns:
        Activities ordered by scheduled time (unscheduled last), then explicit
        ``order``, then creation time.
    """
    return trip.activities.select_related(
        "location",
        "pin",
        "pin__location",
        "added_by__user",
        "child_trip",
    ).order_by(
        F("scheduled_at").asc(nulls_last=True),
        "order",
        "created",
    )


def get_activity(trip: Trip, activity_id: int) -> TripActivity:
    """Return one of the trip's own activities.

    Args:
        trip: The trip that must own the activity.
        activity_id: The activity's primary key.

    Returns:
        The activity.

    Raises:
        TripNotFoundError: No such activity on this trip.
    """
    activity = TripActivity.objects.filter(id=activity_id, trip=trip).select_related("location", "pin", "added_by__user", "child_trip").first()
    if activity is None:
        raise TripNotFoundError(ACTIVITY_NOT_FOUND)
    return activity


def compute_activity_index_map(activities: Iterable[TripActivity]) -> dict[int, int]:
    """Return ``{activity_id: map_index}`` for activities that get a numbered map marker.

    Completed activities and activities whose location is hidden or has no
    coordinates are skipped, so the remaining numbers stay contiguous from 1.

    Args:
        activities: The trip's activities in itinerary order.

    Returns:
        Mapping of activity id to its 1-based marker number.
    """
    index_map: dict[int, int] = {}
    idx = 1
    for act in activities:
        if activity_coords(act) is not None and not act.location_hidden and act.status != TripActivity.STATUS_COMPLETED:
            index_map[act.id] = idx
            idx += 1
    return index_map


def expand_trip_dates(trip: Trip, activity_date: datetime.date) -> None:
    """Widen the trip's persisted date range to include *activity_date*.

    Args:
        trip: The trip to widen.
        activity_date: A date that must fall inside the trip's range.
    """
    changed = False
    if trip.start_date is None or activity_date < trip.start_date:
        trip.start_date = activity_date
        changed = True
    if trip.end_date is None or activity_date > trip.end_date:
        trip.end_date = activity_date
        changed = True
    if changed:
        trip.save(update_fields=["start_date", "end_date", "updated"])


def parse_scheduled_at(date_str: str | None, time_str: str | None) -> datetime.datetime | None:
    """Combine separate date and time strings into an aware datetime.

    If only a date is provided, midnight is used so the caller can distinguish
    "date only" from "date + time" by inspecting the time component.

    Args:
        date_str: An ISO date (``YYYY-MM-DD``), or None/blank.
        time_str: An ISO time (``HH:MM``), or None/blank.

    Returns:
        The combined aware datetime, or None when no usable date was given.
    """
    if not date_str:
        return None
    try:
        parsed_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None
    if time_str:
        try:
            parsed_time = datetime.time.fromisoformat(time_str)
        except ValueError:
            parsed_time = datetime.time(0, 0)
    else:
        parsed_time = datetime.time(0, 0)
    return timezone.make_aware(datetime.datetime.combine(parsed_date, parsed_time))


def resolve_activity_place(body: Mapping[str, Any], profile: Profile) -> tuple[Location | None, Pin | None]:
    """Resolve an activity's target place from submitted location fields.

    Priority: the caller's own selected pin, then a shared location, then
    supplied coordinates. Raw coordinates get (or reuse) a ``Location`` row.

    Args:
        body: Submitted fields - any of ``pin_uuid``/``pin_slug``,
            ``location_uuid``/``location_slug``, ``geocoded_lat``/``geocoded_lng``
            (plus optional ``geocoded_name``/``title``).
        profile: The submitting profile; pin lookups are scoped to their own pins.

    Returns:
        The resolved ``(location, pin)`` pair - either or both may be None.
    """
    import uuid as uuid_module

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

    pin_ref = (body.get("pin_uuid") or body.get("pin_slug") or "").strip()
    if pin_ref:
        # The shared location-search engine identifies pins by slug (falling
        # back to the uuid when a pin has no slug), so accept either form.
        pin_qs = Pin.objects.filter(profile=profile).select_related("location")
        pin = pin_qs.filter(slug=pin_ref).first()
        if pin is None:
            try:
                pin = pin_qs.filter(uuid=uuid_module.UUID(pin_ref)).first()
            except ValueError:
                pin = None
        if pin is not None:
            return pin.location, pin

    location_ref = (body.get("location_uuid") or body.get("location_slug") or "").strip()
    if location_ref:
        location = Location.objects.filter(uuid=location_ref).first() or Location.objects.filter(slug=location_ref).first()
        if location is not None:
            return location, None

    geocoded_lat = (body.get("geocoded_lat") or "").strip()
    geocoded_lng = (body.get("geocoded_lng") or "").strip()
    if geocoded_lat and geocoded_lng:
        try:
            lat = float(geocoded_lat)
            lng = float(geocoded_lng)
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return None, None
            name = (body.get("geocoded_name") or body.get("title") or f"{lat:.6f}, {lng:.6f}").strip()

            location, _ = Location.objects.get_or_create(
                latitude=lat,
                longitude=lng,
                defaults={"official_name": name or "Activity Location"},
            )
            # Wikis are user-created only; a trip activity location gets one
            # when someone explicitly creates it from a pin detail page.
            return location, None
        except (ValueError, TypeError):
            pass

    return None, None


def create_visit_entries_for_completed_activity(trip: Trip, activity: TripActivity, completer: Profile) -> None:
    """Log the completer's visit and suggest visits to everyone whose effective RSVP was yes.

    The completer's visit is logged immediately since completing the activity
    IS their confirmation. Everyone else gets a suggestion to accept or reject,
    because the system can't know they actually went. An activity-level RSVP
    override takes precedence over the member's trip-wide RSVP.

    Args:
        trip: The trip the activity belongs to.
        activity: The activity just marked completed.
        completer: The profile who marked it completed.
    """
    coords = activity_coords(activity)
    if coords is None:
        return
    lat, lng = coords

    if visit_logging_allowed(completer):
        pin = get_or_create_pin_at(completer, location=activity.location, latitude=lat, longitude=lng)
        PinVisit.objects.create(pin=pin, visited_at=activity.scheduled_at, source=VisitSource.TRIP)
        sync_last_visited(pin)
        add_visited_status(pin)

    if activity.scheduled_at is not None:
        other_memberships = list(TripMembership.objects.filter(trip=trip).exclude(profile=completer).select_related("profile"))
        overrides = dict(
            TripActivityRSVP.objects.filter(
                activity=activity,
                membership_id__in=[membership.id for membership in other_memberships],
            ).values_list("membership_id", "rsvp"),
        )
        other_yes = [membership for membership in other_memberships if overrides.get(membership.id, membership.rsvp) == TripMembership.RSVP_YES]
        for membership in other_yes:
            create_visit_suggestion(
                suggested_to=membership.profile,
                suggested_by=completer,
                visited_at=activity.scheduled_at,
                location=activity.location,
                latitude=lat,
                longitude=lng,
                candidate_profiles=[m.profile for m in other_yes if m.profile_id != membership.profile_id],
                trip_activity=activity,
            )


def build_activity_rows(trip: Trip, viewer: Profile, *, include_legs: bool = True) -> list[dict[str, Any]]:
    """Build one render row per activity: index, votes, RSVP, permissions, visibility, leg.

    The single source of the activities-panel context and of the external
    API's activity list, so the two can never diverge on what a viewer is
    allowed to see or manage.

    Args:
        trip: The trip whose activities are being rendered.
        viewer: The profile viewing them.
        include_legs: When False, driving legs are omitted entirely and no
            routing call is attempted. The external list endpoint defaults to
            False so a plain list fetch never triggers OSRM traffic.

    Returns:
        One dict per activity in itinerary order, carrying the activity plus
        ``index``, ``vote_up``/``vote_down``/``user_vote``, ``rsvp``/
        ``rsvp_is_override``/``trip_rsvp``, ``can_manage``,
        ``effective_location_hidden``, ``pin_slug``, ``has_coords`` and ``leg``.
    """
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities

    activities = list(activity_queryset(trip))
    index_map = compute_activity_index_map(activities)

    # The "Added by" attribution shows even when the adder's privacy settings
    # don't permit this viewer to see their name/avatar (distinct from the
    # location-visibility gate below, which hides the activity's *location* -
    # this only masks who gets credit for adding it).
    # select_related gives each activity its own added_by instance even for
    # the same underlying profile, so resolve once per distinct adder and
    # re-point every activity at that same (now-mutated) instance.
    distinct_adders: dict[int, Profile] = {}
    for act in activities:
        adder = act.added_by
        if adder is not None:
            distinct_adders.setdefault(adder.pk, adder)
    if distinct_adders:
        resolve_visible_identities(viewer, list(distinct_adders.values()))
        for act in activities:
            if act.added_by is not None:
                act.added_by = distinct_adders[act.added_by.pk]

    activity_ids = [a.id for a in activities]
    raw_votes = TripActivityVote.objects.filter(activity_id__in=activity_ids).values(
        "activity_id",
        "profile_id",
        "vote",
    )
    up_counts: dict[int, int] = {}
    down_counts: dict[int, int] = {}
    user_votes: dict[int, str] = {}
    for v in raw_votes:
        aid = v["activity_id"]
        if v["vote"] == "up":
            up_counts[aid] = up_counts.get(aid, 0) + 1
        else:
            down_counts[aid] = down_counts.get(aid, 0) + 1
        if v["profile_id"] == viewer.id:
            user_votes[aid] = v["vote"]

    viewer_membership = TripMembership.objects.filter(trip=trip, profile=viewer).first()
    activity_rsvp_overrides = (
        dict(
            TripActivityRSVP.objects.filter(
                activity_id__in=activity_ids,
                membership=viewer_membership,
            ).values_list("activity_id", "rsvp"),
        )
        if viewer_membership
        else {}
    )
    trip_rsvp = viewer_membership.rsvp if viewer_membership else None

    # Which activities have their location hidden from this viewer, per the
    # adder's trip_pin_location_visibility privacy setting.
    viewer_hidden = viewer_hidden_activity_ids(activities, viewer)

    viewer_is_organizer = is_organizer(viewer, trip)
    viewer_has_joined = has_joined(viewer, trip)
    rows: list[dict[str, Any]] = [
        {
            "activity": act,
            "index": index_map.get(act.id),
            "vote_up": up_counts.get(act.id, 0),
            "vote_down": down_counts.get(act.id, 0),
            "user_vote": user_votes.get(act.id),
            "rsvp": activity_rsvp_overrides.get(act.id, trip_rsvp),
            "rsvp_is_override": act.id in activity_rsvp_overrides,
            "trip_rsvp": trip_rsvp,
            "can_manage": viewer_has_joined and (act.added_by_id == viewer.id or viewer_is_organizer),
            "effective_location_hidden": act.location_hidden or (act.id in viewer_hidden),
            # Tested on the loaded relation rather than ``pin_id`` so the
            # ownership check below is provably reached with a real Pin.
            "pin_slug": act.pin.slug if (act.pin is not None and act.pin.profile_id == viewer.id) else None,
            "has_coords": activity_coords(act) is not None,
        }
        for act in activities
    ]

    _attach_legs(rows, viewer, include_legs=include_legs)
    return rows


def _attach_legs(rows: list[dict[str, Any]], viewer: Profile, *, include_legs: bool) -> None:
    """Attach the driving leg arriving at each activity, or ``None``.

    Legs are only computed between consecutive non-completed stops whose
    coordinates this viewer may see - a hidden location must not leak even as
    a distance. Nothing is routed at all when ``include_legs`` is False or the
    viewer has external lookups turned off.

    Args:
        rows: Render rows from :func:`build_activity_rows`, mutated in place.
        viewer: The viewing profile, whose ``external_apis_enabled`` gates routing.
        include_legs: False to skip routing entirely.
    """
    if not include_legs:
        for item in rows:
            item["leg"] = None
        return

    from urbanlens.dashboard.services.trip_legs import compute_legs

    leg_stops: list[tuple[int, tuple[float, float]]] = []
    for item in rows:
        act = item["activity"]
        if act.status == TripActivity.STATUS_COMPLETED or item["effective_location_hidden"]:
            continue
        coords = activity_coords(act)
        if coords is not None:
            leg_stops.append((act.id, coords))
    legs = compute_legs(leg_stops) if viewer.external_apis_enabled and len(leg_stops) >= 2 else {}
    for item in rows:
        item["leg"] = legs.get(item["activity"].id)


def create_activity(
    trip: Trip,
    actor: Profile,
    *,
    title: Any = None,
    notes: Any = None,
    scheduled_at: datetime.datetime | None = None,
    scheduled_end: datetime.datetime | None = None,
    place: Mapping[str, Any] | None = None,
    child_trip_uuid: Any = None,
    status: Any = TripActivity.STATUS_PROPOSED,
    location_hidden: Any = False,
) -> TripActivity:
    """Add an activity to a trip's itinerary.

    Args:
        trip: The trip to add to.
        actor: The profile adding it.
        title: Optional free-text title; blank becomes None.
        notes: Optional free-text notes; blank becomes None.
        scheduled_at: When the activity starts, or None.
        scheduled_end: When it ends, or None.
        place: Location-resolution fields (see :func:`resolve_activity_place`).
        child_trip_uuid: Uuid of one of *actor's own* trips to nest here.
        status: ``proposed`` or ``confirmed``; anything else becomes ``proposed``.
        location_hidden: Whether to hide the location from the trip map.

    Returns:
        The created activity.

    Raises:
        TripPermissionError: The actor may not add activities to this trip.
        TripValidationError: The notes exceed the shared text limit.
        TripQuotaError: The trip is already at ``max_trip_activities``.
    """
    require_perform(actor, trip, trip.allow_add_activities, ADD_ACTIVITY_DENIED)

    clean_title = _clean_text(title)
    clean_notes = _clean_text(notes)
    length_error = text_length_error(clean_notes, MAX_TRIP_ACTIVITY_NOTES_LENGTH, "Notes")
    if length_error:
        raise TripValidationError(length_error)

    location, pin = resolve_activity_place(place or {}, actor)
    child_trip = _resolve_child_trip(child_trip_uuid, actor)

    clean_status = str(status or "").strip()
    if clean_status not in SETTABLE_STATUSES:
        clean_status = TripActivity.STATUS_PROPOSED

    max_activities = SiteSettings.get_current().max_trip_activities
    if max_activities > 0 and trip.activities.count() >= max_activities:
        raise TripQuotaError(f"This trip already has the maximum of {max_activities} activities.")

    activity = TripActivity.objects.create(
        trip=trip,
        location=location,
        pin=pin,
        added_by=actor,
        title=clean_title,
        notes=clean_notes,
        scheduled_at=scheduled_at,
        scheduled_end=scheduled_end,
        order=trip.activities.count(),
        status=clean_status,
        child_trip=child_trip,
        location_hidden=_as_bool(location_hidden),
    )
    # Putting a place on the itinerary reveals it to every member - that counts
    # in the sharer's reshare chain like any other pin share.
    from urbanlens.dashboard.services.trip_share_tracking import record_trip_activity_shares

    record_trip_activity_shares(activity)
    return activity


def _resolve_child_trip(child_trip_uuid: Any, actor: Profile) -> Trip | None:
    """Resolve a nested child trip, scoped to the linking user's own trips.

    Without this scoping any authenticated user could link an arbitrary trip
    they have no access to, and its activities (titles, coordinates, schedule)
    would render as ghost markers on this trip's map for every member.

    Args:
        child_trip_uuid: The submitted uuid, possibly blank/None.
        actor: The linking profile.

    Returns:
        The matching trip the actor belongs to, or None.
    """
    ref = str(child_trip_uuid or "").strip()
    if not ref:
        return None
    return Trip.objects.filter(uuid=ref, profiles=actor).first()


def update_activity(trip: Trip, actor: Profile, activity_id: int, *, changes: Mapping[str, Any]) -> TripActivity:
    """Apply a presence-keyed partial update to an activity.

    Only keys actually present in *changes* are touched, so the external API's
    PATCH semantics and the internal form's full-replace semantics are the
    same call - the internal controller simply supplies every key.

    The permission check runs *before* the activity is looked up, so a caller
    without edit rights gets the same 403 whether or not the id exists and
    cannot use this endpoint to enumerate activity ids.

    Args:
        trip: The trip owning the activity.
        actor: The profile making the change.
        activity_id: Primary key of the activity to update.
        changes: Any of ``title``, ``notes``, ``scheduled_at``,
            ``scheduled_end``, ``place``, ``status``, ``child_trip_uuid``,
            ``location_hidden``.

    Returns:
        The saved activity.

    Raises:
        TripPermissionError: The actor may not edit activities on this trip.
        TripNotFoundError: No such activity on this trip.
        TripValidationError: The notes exceed the shared text limit.
    """
    require_perform(actor, trip, trip.allow_edit_activities, EDIT_ACTIVITY_DENIED)
    activity = get_activity(trip, activity_id)

    if "title" in changes:
        activity.title = _clean_text(changes["title"])
    if "notes" in changes:
        clean_notes = _clean_text(changes["notes"])
        length_error = text_length_error(clean_notes, MAX_TRIP_ACTIVITY_NOTES_LENGTH, "Notes")
        if length_error:
            raise TripValidationError(length_error)
        activity.notes = clean_notes
    if "scheduled_at" in changes:
        activity.scheduled_at = changes["scheduled_at"]
    if "scheduled_end" in changes:
        activity.scheduled_end = changes["scheduled_end"]
    if "place" in changes:
        activity.location, activity.pin = resolve_activity_place(changes["place"] or {}, actor)
    if "status" in changes:
        new_status = str(changes["status"] or "").strip()
        if new_status in SETTABLE_STATUSES:
            activity.status = new_status
    if "child_trip_uuid" in changes:
        activity.child_trip = _resolve_child_trip(changes["child_trip_uuid"], actor)
    # location_hidden may only be changed by the activity creator or an organizer.
    if "location_hidden" in changes and (activity.added_by_id == actor.id or is_organizer(actor, trip)):
        activity.location_hidden = _as_bool(changes["location_hidden"])

    activity.save()

    if activity.status == TripActivity.STATUS_CONFIRMED and activity.scheduled_at:
        expand_trip_dates(trip, activity.scheduled_at.date())
    return activity


def delete_activity(trip: Trip, actor: Profile, activity_id: int) -> None:
    """Delete one activity from a trip.

    Args:
        trip: The trip owning the activity.
        actor: The profile deleting it.
        activity_id: Primary key of the activity to delete.

    Raises:
        TripPermissionError: The actor may not delete activities on this trip.
        TripNotFoundError: No such activity on this trip.
    """
    require_perform(actor, trip, trip.allow_edit_activities, DELETE_ACTIVITY_DENIED)
    get_activity(trip, activity_id).delete()


def set_activity_position(trip: Trip, actor: Profile, activity_id: int, *, lat: float, lng: float) -> tuple[float, float]:
    """Save a map-drag position override for one activity.

    Only ``lat_override``/``lng_override`` change - the underlying Pin and
    Location coordinates are never touched.

    Two deliberate behavior changes relative to the endpoint this replaced
    (``controllers.trip.TripActivityPositionView``), applied to the internal
    surface as well as the external one:

    1. It now requires the same permission as editing activities generally.
       Previously it checked only trip *membership*, which admitted
       invited-but-not-joined members - meaning anyone who had merely been
       invited could move any marker on the trip's map.
    2. Coordinates are bounds-checked. Previously any float was accepted and
       persisted, so a marker could be parked at latitude 5000.

    Args:
        trip: The trip owning the activity.
        actor: The profile dragging the marker.
        activity_id: Primary key of the activity being repositioned.
        lat: New latitude, -90..90.
        lng: New longitude, -180..180.

    Returns:
        The saved ``(lat, lng)`` pair.

    Raises:
        TripPermissionError: The actor may not edit activities on this trip.
        TripNotFoundError: No such activity on this trip.
        TripValidationError: The coordinates are non-numeric or out of range.
    """
    require_perform(actor, trip, trip.allow_edit_activities, MOVE_ACTIVITY_DENIED)

    try:
        lat_value = float(lat)
        lng_value = float(lng)
    except (TypeError, ValueError) as exc:
        raise TripValidationError("lat and lng must be numbers.") from exc
    if not (-90 <= lat_value <= 90) or not (-180 <= lng_value <= 180):
        raise TripValidationError("lat must be between -90 and 90, and lng between -180 and 180.")

    activity = get_activity(trip, activity_id)
    activity.lat_override = lat_value
    activity.lng_override = lng_value
    activity.save(update_fields=["lat_override", "lng_override", "updated"])
    return lat_value, lng_value


def set_activity_vote(trip: Trip, actor: Profile, activity_id: int, *, vote: str | None) -> None:
    """Set (or clear) the actor's vote on a proposed activity.

    An explicit value rather than a toggle: a mobile client retrying over a
    flaky connection would otherwise flip its own vote back off.

    Args:
        trip: The trip owning the activity.
        actor: The voting profile.
        activity_id: Primary key of the activity being voted on.
        vote: ``"up"``, ``"down"``, or None to clear.

    Raises:
        TripPermissionError: The actor has not joined the trip.
        TripNotFoundError: No such activity on this trip.
        TripValidationError: The activity is not proposed, or the value is invalid.
    """
    require_perform(actor, trip, Trip.PERM_EVERYONE, CONTRIBUTE_DENIED)
    activity = get_activity(trip, activity_id)

    if activity.status != TripActivity.STATUS_PROPOSED:
        raise TripValidationError("Voting is only available for proposed activities.")

    if not vote:
        TripActivityVote.objects.filter(activity=activity, profile=actor).delete()
        return
    if vote not in {TripActivityVote.VOTE_UP, TripActivityVote.VOTE_DOWN}:
        raise TripValidationError("Invalid vote value.")
    TripActivityVote.objects.update_or_create(
        activity=activity,
        profile=actor,
        defaults={"vote": vote},
    )


def set_activity_status(trip: Trip, actor: Profile, activity_id: int, *, status: str | None) -> TripActivity:
    """Set an activity's status, or toggle proposed/confirmed when no valid value is given.

    Args:
        trip: The trip owning the activity.
        actor: The profile changing the status.
        activity_id: Primary key of the activity to change.
        status: ``proposed``/``confirmed``, or None/invalid to toggle between them.

    Returns:
        The saved activity.

    Raises:
        TripPermissionError: The actor has not joined the trip.
        TripNotFoundError: No such activity on this trip.
    """
    require_perform(actor, trip, Trip.PERM_EVERYONE, CONTRIBUTE_DENIED)
    activity = get_activity(trip, activity_id)

    new_status = str(status or "").strip()
    if new_status not in SETTABLE_STATUSES:
        new_status = TripActivity.STATUS_CONFIRMED if activity.status == TripActivity.STATUS_PROPOSED else TripActivity.STATUS_PROPOSED

    activity.status = new_status
    activity.save(update_fields=["status", "updated"])

    if new_status == TripActivity.STATUS_CONFIRMED and activity.scheduled_at:
        expand_trip_dates(trip, activity.scheduled_at.date())
    return activity


def set_activity_rsvp(trip: Trip, actor: Profile, activity_id: int, *, rsvp: str | None) -> None:
    """Set or clear the actor's RSVP override for one activity.

    Clearing removes the override so the activity immediately inherits the
    member's current trip-wide RSVP again.

    Args:
        trip: The trip owning the activity.
        actor: The responding profile.
        activity_id: Primary key of the activity being answered.
        rsvp: One of ``TripMembership.RSVP_CHOICES``, or None/blank to clear.

    Raises:
        TripPermissionError: The actor has not joined the trip.
        TripValidationError: The RSVP value is not a valid choice.
        TripNotFoundError: No such activity, or the actor has no membership row.
    """
    require_joined(actor, trip, ACTIVITY_RSVP_DENIED)
    activity = get_activity(trip, activity_id)

    membership = TripMembership.objects.for_trip_and_profile(trip, actor).first()
    if membership is None:
        raise TripNotFoundError("You are not a member of this trip.")

    value = (rsvp or "").strip()
    valid_rsvps = {choice[0] for choice in TripMembership.RSVP_CHOICES}
    if value and value not in valid_rsvps:
        raise TripValidationError("Invalid RSVP value.")

    if value:
        TripActivityRSVP.objects.update_or_create(
            activity=activity,
            membership=membership,
            defaults={"rsvp": value},
        )
    else:
        TripActivityRSVP.objects.filter(activity=activity, membership=membership).delete()


def complete_activity(trip: Trip, actor: Profile, activity_id: int, *, completed_date: datetime.date | None = None) -> TripActivity:
    """Mark an activity completed, snapping a future date back to today.

    Completing implies the activity was confirmed to have happened, so it
    expands the trip's date range the same way confirming it would - even if
    it was only ever "proposed" beforehand.

    Args:
        trip: The trip owning the activity.
        actor: The profile marking it complete.
        activity_id: Primary key of the activity to complete.
        completed_date: The date it happened; clamped to today and defaulted
            to today when omitted or unparseable.

    Returns:
        The saved activity.

    Raises:
        TripPermissionError: The actor has not joined the trip.
        TripNotFoundError: No such activity on this trip.
    """
    require_perform(actor, trip, Trip.PERM_EVERYONE, CONTRIBUTE_DENIED)
    activity = get_activity(trip, activity_id)

    already_completed = activity.status == TripActivity.STATUS_COMPLETED
    today = datetime.date.today()
    effective_date = min(completed_date, today) if completed_date is not None else today

    activity.scheduled_at = timezone.make_aware(
        datetime.datetime.combine(
            effective_date,
            activity.scheduled_at.time() if activity.scheduled_at else datetime.time(0, 0),
        ),
    )
    activity.status = TripActivity.STATUS_COMPLETED
    activity.save(update_fields=["status", "scheduled_at", "updated"])
    expand_trip_dates(trip, effective_date)
    if not already_completed:
        create_visit_entries_for_completed_activity(trip, activity, actor)
    return activity


def reorder_activities(trip: Trip, actor: Profile, order: Sequence[int]) -> None:
    """Apply an explicit ordering to the trip's non-completed activities.

    Only ever accepts an exact permutation of the trip's own current
    non-completed activities - never partial, never containing another trip's
    ids - so a stale or tampered order can't silently drop or hijack anything.

    Args:
        trip: The trip being reordered.
        actor: The profile reordering it.
        order: Activity ids in their new order.

    Raises:
        TripPermissionError: The actor may not reorder activities.
        TripValidationError: The order is not an exact permutation.
    """
    require_perform(actor, trip, trip.allow_edit_activities, REORDER_ACTIVITY_DENIED)

    valid_ids = set(trip.activities.exclude(status=TripActivity.STATUS_COMPLETED).values_list("id", flat=True))
    if not order or len(order) != len(valid_ids) or set(order) != valid_ids:
        raise TripValidationError("The order must include exactly the trip's current non-completed activities.")

    for position, activity_id in enumerate(order):
        TripActivity.objects.filter(id=activity_id, trip=trip).update(order=position)
