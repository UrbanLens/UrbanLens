"""Trip ↔ Google Calendar event conversion and sync orchestration.
Pure mapping helpers (:func:`trip_to_event_body`, :func:`event_to_trip_kwargs`) are kept free of I/O so they can be property-tested; the ``import_*`` / ``export_*`` functions do the API calls and bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models import DateTimeField, F, Q, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import (
    ACTIVITY_ID_EVENT_PROPERTY,
    TRIP_UUID_EVENT_PROPERTY,
    CalendarEventExistsError,
    CalendarEventNotFoundError,
    EventListing,
    GoogleCalendarGateway,
    client_event_id,
)
from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.core.site_urls import absolute_url
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.trips.trip_visibility import masked_activity_title, viewer_hidden_activity_ids

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

# How far ahead the import dialog looks for events.
IMPORT_WINDOW_DAYS = 365

#: The most events the import dialog lists, and so the most one preview or import may name.
MAX_IMPORTABLE_EVENTS = 500

RECONNECT_MESSAGE = "Your Google Calendar connection has expired. Please reconnect."
GATEWAY_FAILURE_MESSAGE = "Google Calendar could not be reached. Please try again in a moment."
#: The calendar budget was spent before anything of the trip could be written.
CALENDAR_BUSY_MESSAGE = "Google Calendar is busy right now. Please try again in a minute."


class TooManyEventsError(ValueError):
    """More distinct events were named than the import dialog can list."""


def normalize_event_ids(raw: Sequence[str]) -> list[str]:
    """The distinct, non-blank event ids a request named, in order.

    Args:
        raw: Posted ids.

    Returns:
        The ids, deduplicated.

    Raises:
        TooManyEventsError: More than :data:`MAX_IMPORTABLE_EVENTS` distinct ids.
    """
    ids = list(dict.fromkeys(event_id.strip() for event_id in raw if event_id and event_id.strip()))
    if len(ids) > MAX_IMPORTABLE_EVENTS:
        raise TooManyEventsError(f"{len(ids)} events named; at most {MAX_IMPORTABLE_EVENTS} may be imported at once.")
    return ids


def _import_window(gateway: GoogleCalendarGateway) -> EventListing:
    now = timezone.now()
    return gateway.list_events(
        time_min=now - datetime.timedelta(days=1),
        time_max=now + datetime.timedelta(days=IMPORT_WINDOW_DAYS),
        limit=MAX_IMPORTABLE_EVENTS,
    )


def _events_by_id(gateway: GoogleCalendarGateway, event_ids: Collection[str]) -> dict[str, dict[str, Any]]:
    """The named events, read from the same listing the dialog offered them from.

    One paged listing rather than a request per id: the ids came from that listing, and what Google returns
    is still the only event data trusted (the client submits ids, never content).

    Returns:
        Event resources by id; a named id missing from it has left the calendar or the import window.
    """
    if not event_ids:
        return {}
    wanted = set(event_ids)
    return {event["id"]: event for event in _import_window(gateway).events if event.get("id") in wanted}


_MAX_TRIP_NAME_LENGTH = 255

# Length of the calendar event created for an activity that has a start time
# but no explicit end time.
DEFAULT_ACTIVITY_EVENT_DURATION = datetime.timedelta(hours=2)


def trip_to_event_body(
    trip: Trip,
    *,
    trip_url: str | None = None,
    hidden_activity_ids: Collection[int] | None = None,
    owns_event: bool = True,
) -> dict[str, Any]:
    """Convert a trip into a Google Calendar all-day event payload.
    Trips carry dates (not times), so they map to all-day events.

    An update is a PATCH, which keeps every field the body leaves out, so a location this export withholds is sent as
    ``""`` rather than left out (P335).

    Args:
        trip: The trip to export.
        trip_url: Optional absolute URL of the trip page to append to the event description.
        hidden_activity_ids: Ids of activities whose location the *exporting* viewer may not see, from :func:`~urbanlens.dashboard.services.trips.trip_visibility.viewer_hidden_activity_ids`.
        owns_event: Whether UrbanLens made the event, so a location the trip no longer has is cleared too. False for an
            event an import linked, whose own location is left alone unless the trip has one withheld from the viewer.

    Returns:
        Event resource payload for the Calendar API.

    Raises:
        ValueError: When the trip has no start date (no dates and no scheduled activities)."""
    start = trip.effective_start_date
    if start is None:
        raise ValueError("Trip has no start date or scheduled activities - set dates before exporting.")
    end = trip.effective_end_date or start
    end = max(end, start)

    description = (trip.description or "").strip()
    if trip_url:
        description = f"{description}\n\n{trip_url}".strip()

    body: dict[str, Any] = {
        "summary": trip.name,
        "description": description,
        "start": {"date": start.isoformat()},
        "end": {"date": (end + datetime.timedelta(days=1)).isoformat()},
        "extendedProperties": {"private": {TRIP_UUID_EVENT_PROPERTY: str(trip.uuid)}},
    }
    _set_location(body, _trip_location_string(trip, hidden_activity_ids=hidden_activity_ids), owns_event=owns_event)
    return body


def _set_location(body: dict[str, Any], location: str | None, *, owns_event: bool) -> None:
    """Put an event's ``location`` in *body*: the location, ``""`` to clear it, or nothing to leave the event's own.

    Args:
        body: The event payload being built.
        location: From :func:`_activity_location_string` or :func:`_trip_location_string`.
        owns_event: Whether UrbanLens made the event, so having no location clears it rather than leaving it.
    """
    if location is not None or owns_event:
        body["location"] = location or ""


def _location_withheld(activity: TripActivity, hidden_activity_ids: Collection[int] | None) -> bool:
    """Whether the exporting viewer may not see *activity*'s location."""
    return activity.location_hidden or (hidden_activity_ids is not None and activity.pk in hidden_activity_ids)


def _activity_location_string(activity: TripActivity, *, hidden_activity_ids: Collection[int] | None = None) -> str | None:
    """Human-readable location for an activity's calendar event.

    Args:
        activity: The TripActivity to describe.
        hidden_activity_ids: Ids of activities whose location the exporting viewer may not see.

    Returns:
        The address, else the coordinates; ``""`` when the activity has a location the exporting viewer may not see;
        None when it has none."""
    location = activity.location or (activity.pin.location if activity.pin else None)
    lat = activity.lat_override if activity.lat_override is not None else (float(location.latitude) if location else None)
    lng = activity.lng_override if activity.lng_override is not None else (float(location.longitude) if location else None)
    if location is not None and location.address:
        shown = location.address
    elif lat is not None and lng is not None:
        shown = f"{lat:.6f}, {lng:.6f}"
    else:
        return None
    return "" if _location_withheld(activity, hidden_activity_ids) else shown


def _trip_location_string(trip: Trip, *, hidden_activity_ids: Collection[int] | None = None) -> str | None:
    """Human-readable location for the trip-level calendar event.
    Uses the trip's first activity (by schedule, then manual order) that has a shareable location, so the exported all-day event points at where the trip starts.

    Args:
        trip: The trip being exported.
        hidden_activity_ids: Ids of activities whose location the exporting viewer may not see - see :func:`_activity_location_string`.

    Returns:
        The first location the exporting viewer may see; ``""`` when every activity's location is withheld from them;
        None when no activity has a location."""
    if trip.pk is None:
        # Unsaved trips (e.g. pure-mapping property tests) have no activities.
        return None
    withheld = False
    for activity in trip.activities.select_related("location", "pin__location"):
        location_string = _activity_location_string(activity, hidden_activity_ids=hidden_activity_ids)
        if location_string:
            return location_string
        withheld = withheld or location_string == ""
    return "" if withheld else None


def activity_to_event_body(
    activity: TripActivity,
    *,
    trip_url: str | None = None,
    hidden_activity_ids: Collection[int] | None = None,
    owns_event: bool = True,
) -> dict[str, Any] | None:
    """Convert one scheduled trip activity into a timed calendar event payload.
    Activities without a scheduled start cannot be placed on a calendar and yield None.

    A location withheld from the exporting viewer is sent as ``""``, so an update clears it (P335), and the title is
    masked the way the activities panel masks it, since ``effective_title`` falls back to the place's name or address.

    Args:
        activity: The TripActivity to export (with ``trip`` loaded).
        trip_url: Optional absolute URL of the trip page to append to the event description.
        hidden_activity_ids: Ids of activities whose location the exporting viewer may not see - see :func:`_activity_location_string`.
        owns_event: Whether UrbanLens made the event - see :func:`trip_to_event_body`.

    Returns:
        Event resource payload, or None when the activity is unscheduled."""
    if activity.scheduled_at is None:
        return None
    start = activity.scheduled_at
    end = activity.scheduled_end
    if end is None or end <= start:
        end = start + DEFAULT_ACTIVITY_EVENT_DURATION

    description = (activity.notes or "").strip()
    if trip_url:
        description = f"{description}\n\n{trip_url}".strip()

    title = masked_activity_title(activity, hidden=_location_withheld(activity, hidden_activity_ids))
    body: dict[str, Any] = {
        "summary": f"{activity.trip.name}: {title}",
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {
            "private": {
                TRIP_UUID_EVENT_PROPERTY: str(activity.trip.uuid),
                ACTIVITY_ID_EVENT_PROPERTY: str(activity.pk),
            },
        },
    }
    _set_location(body, _activity_location_string(activity, hidden_activity_ids=hidden_activity_ids), owns_event=owns_event)
    return body


def _parse_event_date(part: dict[str, Any] | None) -> tuple[datetime.date | None, bool]:
    """Parse the date from one side of an event's start/end structure.

    Args:
        part: The event's ``start`` or ``end`` dict (``{"date": ...}`` for all-day events, ``{"dateTime": ...}`` for timed ones).

    Returns:
        Tuple of (parsed date or None, whether it was an all-day ``date``)."""
    if not part:
        return None, False
    raw_date = part.get("date")
    if raw_date:
        try:
            return datetime.date.fromisoformat(raw_date), True
        except ValueError:
            return None, True
    raw_datetime = part.get("dateTime")
    if raw_datetime:
        try:
            return datetime.datetime.fromisoformat(raw_datetime).date(), False
        except ValueError:
            return None, False
    return None, False


def event_to_trip_kwargs(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Google Calendar event into ``Trip.objects.create`` kwargs.
    Cancelled events and events without a parsable start are rejected.

    Args:
        event: Event resource dict from the Calendar API.

    Returns:
        Kwargs for creating a Trip (name, description, start_date, end_date), or None when the event cannot become a trip."""
    if event.get("status") == "cancelled":
        return None

    start_date, _ = _parse_event_date(event.get("start"))
    if start_date is None:
        return None

    end_date, end_is_all_day = _parse_event_date(event.get("end"))
    if end_date is not None and end_is_all_day:
        # Exclusive all-day end -> inclusive trip end.
        end_date -= datetime.timedelta(days=1)
    if end_date is not None and end_date < start_date:
        end_date = start_date

    name = (event.get("summary") or "").strip() or "Imported calendar event"
    description = (event.get("description") or "").strip() or None

    return {
        "name": name[:_MAX_TRIP_NAME_LENGTH],
        "description": description,
        "start_date": start_date,
        "end_date": end_date or start_date,
    }


def _parse_event_datetime(part: dict[str, Any] | None) -> datetime.datetime | None:
    """Parse the timestamp from one side of a *timed* event's start/end structure.

    Args:
        part: The event's ``start`` or ``end`` dict.

    Returns:
        The parsed datetime, or None for all-day/missing/unparsable values and ones no activity may be scheduled at."""
    from urbanlens.dashboard.models.trips.model import within_activity_schedule

    raw = (part or {}).get("dateTime")
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if within_activity_schedule(parsed) else None


def match_event_attendees(profile: Profile, event: dict[str, Any]) -> tuple[list[Profile], list[str]]:
    """Split an event's attendees into invitable friends and everyone else.
    Non-friends and unknown addresses are returned only as display labels - no account information is revealed beyond what the importer's own calendar already shows.

    Args:
        profile: The importing user's profile.
        event: Event resource dict from the Calendar API.

    Returns:
        Tuple of (friend profiles that can be invited, display labels for the remaining attendees)."""
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.services.auth.email_normalization import find_verified_user_by_email, normalize_email

    own_email = normalize_email(profile.user.email or "")
    friends: list[Profile] = []
    others: list[str] = []
    seen_profile_ids: set[int] = set()

    for attendee in event.get("attendees") or []:
        email = (attendee.get("email") or "").strip()
        label = attendee.get("displayName") or email
        if not email or attendee.get("self") or normalize_email(email) == own_email:
            continue
        if attendee.get("resource"):
            # Meeting rooms and other calendar resources are never people.
            continue
        user = find_verified_user_by_email(email)
        if user is not None:
            attendee_profile = ProfileModel.objects.filter(user=user).first()
            if attendee_profile is not None:
                if attendee_profile.pk == profile.pk or attendee_profile.pk in seen_profile_ids:
                    continue
                if ProfileModel.are_friends(profile, attendee_profile):
                    seen_profile_ids.add(attendee_profile.pk)
                    friends.append(attendee_profile)
                    continue
        if label:
            others.append(label)

    return friends, others


def build_import_preview(account: GoogleCalendarAccount, event_ids: list[str]) -> list[dict[str, Any]]:
    """Build the review-page data for the events selected on the import dialog's first page.
    Each event is re-fetched so only data Google actually returns is trusted.

    Args:
        account: The user's connected calendar account.
        event_ids: Google event ids selected on page one.

    Returns:
        List of preview dicts with ``event_id``, ``summary``, ``trip_kwargs``, ``location``, ``scheduled_at``/``scheduled_end`` (for timed events), ``friends``, ``other_attendees``, and ``skip_reason`` keys.

    Raises:
        TooManyEventsError: More distinct ids than the dialog can list.
        GatewayRequestError: When the calendar cannot be read."""
    gateway = GoogleCalendarGateway(account=account)
    profile = account.profile
    previews: list[dict[str, Any]] = []
    event_ids = normalize_event_ids(event_ids)
    linked = set(TripCalendarLink.objects.filter(profile=profile, google_event_id__in=event_ids).values_list("google_event_id", flat=True))
    events = _events_by_id(gateway, [event_id for event_id in event_ids if event_id not in linked])

    for event_id in event_ids:
        entry: dict[str, Any] = {
            "event_id": event_id,
            "summary": "",
            "trip_kwargs": None,
            "location": "",
            "scheduled_at": None,
            "scheduled_end": None,
            "friends": [],
            "other_attendees": [],
            "skip_reason": "",
        }
        previews.append(entry)

        if event_id in linked:
            entry["skip_reason"] = "Already linked to a trip."
            continue
        event = events.get(event_id)
        if event is None:
            entry["skip_reason"] = "This event is no longer on your calendar in the coming year."
            continue

        entry["summary"] = (event.get("summary") or "").strip() or "(untitled event)"
        if event_originated_from_urbanlens(event):
            entry["skip_reason"] = "Exported from an UrbanLens trip."
            continue

        kwargs = event_to_trip_kwargs(event)
        if kwargs is None:
            entry["skip_reason"] = "No usable dates."
            continue

        entry["trip_kwargs"] = kwargs
        entry["location"] = (event.get("location") or "").strip()
        entry["scheduled_at"] = _parse_event_datetime(event.get("start"))
        entry["scheduled_end"] = _parse_event_datetime(event.get("end"))
        entry["friends"], entry["other_attendees"] = match_event_attendees(profile, event)

    return previews


def _create_activity_from_event(trip: Trip, event: dict[str, Any], profile: Profile) -> TripActivity | None:
    """Create a trip activity carrying the calendar event's location.

    Args:
        trip: The freshly imported trip.
        event: Event resource dict the trip was created from.
        profile: The importing user's profile.

    Returns:
        The created activity, or None when the event has no location."""
    location_text = (event.get("location") or "").strip()
    if not location_text:
        return None
    return TripActivity.objects.create(
        trip=trip,
        added_by=profile,
        title=location_text[:255],
        notes="Location from the imported Google Calendar event.",
        scheduled_at=_parse_event_datetime(event.get("start")),
        scheduled_end=_parse_event_datetime(event.get("end")),
    )


def _invite_participants(trip: Trip, importer: Profile, profile_ids: list[int], skipped: list[str]) -> int:
    """Add confirmed friends as trip members and notify them.
    Every id is re-validated server side: only accepted friends of the importer are ever added, regardless of what the client submitted.

    Args:
        trip: The freshly imported trip.
        importer: The importing user's profile.
        profile_ids: Profile ids the importer confirmed on the review page.
        skipped: Mutable list human-readable skip reasons are appended to.

    Returns:
        The number of members actually added."""
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.services.trips.trip_errors import TripQuotaError
    from urbanlens.dashboard.services.trips.trip_membership import notify_added_to_trip
    from urbanlens.dashboard.services.trips.trip_seats import reserve_trip_seat

    if not profile_ids:
        return 0

    max_members = SiteSettings.get_current().max_trip_members
    invited = 0
    for profile_id in profile_ids:
        invitee = ProfileModel.objects.filter(pk=profile_id).select_related("user").first()
        if invitee is None or invitee.pk == importer.pk:
            continue
        if not ProfileModel.are_friends(importer, invitee):
            # Named toward the importer with the same masking: they picked this
            # person from a list that may itself have shown a placeholder.
            invitee_name = resolve_visible_identity(importer, invitee)["display_name"]
            skipped.append(f"{invitee_name} was not invited because you are not friends on UrbanLens.")
            continue
        try:
            _membership, created = reserve_trip_seat(trip, invitee)
        except TripQuotaError:
            skipped.append(f'"{trip.name}" is full ({max_members} members maximum); some invitations were not sent.')
            break
        if created:
            # Delegates to the canonical implementation rather than duplicating it, so this path
            # can't drift from - or forget to apply, as it once did - the recipient's added_to_trip
            # delivery preference (including NONE, which must suppress the row entirely, not just
            # skip email).
            notify_added_to_trip(importer, invitee, trip)
            invited += 1
    return invited


def event_originated_from_urbanlens(event: dict[str, Any]) -> bool:
    """Whether an event was created by an UrbanLens trip export.

    Args:
        event: Event resource dict.

    Returns:
        True when the event carries the private trip-UUID marker property.
    """
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return bool(private.get(TRIP_UUID_EVENT_PROPERTY))


def list_importable_events(account: GoogleCalendarAccount) -> tuple[list[dict[str, Any]], bool]:
    """Fetch upcoming events from the user's calendar, annotated for the import dialog.

    Args:
        account: The user's connected calendar account.

    Returns:
        ``{"event", "trip_kwargs", "already_linked", "from_urbanlens"}`` dicts in calendar order, and whether
        the calendar held more than :data:`MAX_IMPORTABLE_EVENTS` in the window.

    Raises:
        GatewayRequestError: When the calendar cannot be read."""
    listing = _import_window(GoogleCalendarGateway(account=account))
    events = listing.events

    event_ids = [e.get("id") for e in events if e.get("id")]
    linked_ids = set(
        TripCalendarLink.objects.filter(profile=account.profile, google_event_id__in=event_ids).values_list("google_event_id", flat=True),
    )

    results: list[dict[str, Any]] = []
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        results.append(
            {
                "event": event,
                "trip_kwargs": event_to_trip_kwargs(event),
                "already_linked": event_id in linked_ids,
                "from_urbanlens": event_originated_from_urbanlens(event),
            },
        )
    return results, listing.truncated


def import_events_as_trips(
    account: GoogleCalendarAccount,
    selections: Sequence[str | dict[str, Any]],
    *,
    report_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Trip], list[str], int]:
    """Create trips from the given calendar events on the user's calendar.
    Events are read back from Google's own listing, so only data Google actually returns is trusted (the client submits ids, never event content).

    Args:
        account: The user's connected calendar account.
        selections: Either bare Google event ids, or dicts with ``event_id``, ``create_activity`` (bool, default True), ``invite_profile_ids`` (list of ints, default empty), and ``auto_sync`` (bool, default False) keys.
        report_progress: Called with ``(done, total)`` after each selection.

    Returns:
        Tuple of (created trips, human-readable skip reasons, number of participants invited).

    Raises:
        TooManyEventsError: More distinct events than the dialog can list."""
    gateway = GoogleCalendarGateway(account=account)
    profile = account.profile
    created: list[Trip] = []
    skipped: list[str] = []
    invited_total = 0

    by_id: dict[str, dict[str, Any]] = {}
    for raw_selection in selections:
        selection: dict[str, Any] = {"event_id": raw_selection} if isinstance(raw_selection, str) else raw_selection
        event_id = str(selection.get("event_id") or "").strip()
        if event_id and event_id not in by_id:
            by_id[event_id] = selection
    normalize_event_ids(list(by_id))
    linked = set(TripCalendarLink.objects.filter(profile=profile, google_event_id__in=list(by_id)).values_list("google_event_id", flat=True))
    events = _events_by_id(gateway, [event_id for event_id in by_id if event_id not in linked])

    for done, (event_id, selection) in enumerate(by_id.items(), start=1):
        if report_progress is not None:
            report_progress(done, len(by_id))

        if event_id in linked or TripCalendarLink.objects.already_linked(profile, event_id):
            skipped.append("An event was skipped because it is already linked to a trip.")
            continue

        event = events.get(event_id)
        if event is None:
            skipped.append("An event was skipped because it is no longer on your calendar in the coming year.")
            continue

        if event_originated_from_urbanlens(event):
            skipped.append(f'"{event.get("summary") or "Untitled"}" was skipped because it was exported from an UrbanLens trip.')
            continue

        kwargs = event_to_trip_kwargs(event)
        if kwargs is None:
            skipped.append(f'"{event.get("summary") or "Untitled"}" could not be converted to a trip.')
            continue

        # The already_linked() check above is a read; two concurrent imports of the same event (a
        # double-submit, or two workers) can both pass it.
        # The partial unique on (profile, google_event_id) is what actually decides, so build the
        # whole trip inside one atomic block: a loser rolls the trip back rather than leaving a
        try:
            with transaction.atomic():
                trip = _create_trip_from_event(
                    profile=profile,
                    account=account,
                    event=event,
                    event_id=event_id,
                    kwargs=kwargs,
                    create_activity=bool(selection.get("create_activity", True)),
                    auto_sync=bool(selection.get("auto_sync")),
                )
        except IntegrityError:
            skipped.append("An event was skipped because it is already linked to a trip.")
            continue

        invited_total += _invite_participants(trip, profile, list(selection.get("invite_profile_ids") or []), skipped)
        created.append(trip)

    return created, skipped, invited_total


def import_summary(created: int, skipped: Sequence[str], invited: int) -> tuple[str, str]:
    """The toast level and message reporting an import.

    Args:
        created: How many trips were created.
        skipped: Skip reasons.
        invited: How many participants were invited.

    Returns:
        ``(level, message)``.
    """
    if created:
        message = f"Imported {created} event{'s' if created != 1 else ''} as trips."
        level = "success"
        if invited:
            message += f" Invited {invited} participant{'s' if invited != 1 else ''}."
    else:
        message = "No events were imported."
        level = "warning"
    if skipped:
        message += f" {skipped[0]}" if len(skipped) == 1 else f" {len(skipped)} items were skipped."
    return level, message


def run_calendar_import(profile_id: int, selections: Sequence[dict[str, Any]], *, report_progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Import calendar events as trips for one profile, reporting the outcome rather than raising it.

    The body of the background import: the request that started it has already answered, so every
    outcome - including a dead Google grant - becomes a result the polling dialog can show.

    Args:
        profile_id: The importing profile.
        selections: As :func:`import_events_as_trips` takes them.
        report_progress: Called with ``(done, total)`` as selections are handled.

    Returns:
        ``{"level", "message", "created"}``.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    profile = Profile.objects.filter(pk=profile_id).first()
    account = GoogleCalendarAccount.objects.get_for_profile(profile) if profile is not None else None
    if account is None:
        return {"level": "error", "message": "Connect your Google Calendar first.", "created": 0}
    try:
        created, skipped, invited = import_events_as_trips(account, selections, report_progress=report_progress)
    except GoogleAuthExpiredError:
        account.delete()
        return {"level": "error", "message": RECONNECT_MESSAGE, "created": 0}
    except GatewayRequestError as exc:
        logger.warning("Google Calendar import for profile %s failed: %s", profile_id, exc, exc_info=True)
        return {"level": "error", "message": GATEWAY_FAILURE_MESSAGE, "created": 0}
    except TooManyEventsError:
        return {"level": "error", "message": f"Import at most {MAX_IMPORTABLE_EVENTS} events at a time.", "created": 0}
    level, message = import_summary(len(created), skipped, invited)
    return {"level": level, "message": message, "created": len(created)}


def _create_trip_from_event(
    *,
    profile: Profile,
    account: GoogleCalendarAccount,
    event: dict[str, Any],
    event_id: str,
    kwargs: dict[str, Any],
    create_activity: bool,
    auto_sync: bool,
) -> Trip:
    """Create the trip, its membership, optional activity and calendar link(s).
    Split out of :func:`import_events_to_trips` so the whole unit can run in one transaction and be rolled back as a whole when the event turns out to have been imported concurrently.

    Args:
        profile: The importing profile, who becomes the trip's creator.
        account: The connected calendar account the event came from.
        event: The raw Google Calendar event body.
        event_id: The event's Google id.
        kwargs: Trip field values derived from the event.
        create_activity: Whether to also build an activity from the event.
        auto_sync: Whether the trip-level link should push future changes back.

    Returns:
        The created trip.

    Raises:
        IntegrityError: If this profile already has a link for ``event_id``."""
    trip = Trip.objects.create(creator=profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"rsvp": TripMembership.RSVP_YES})

    activity = _create_activity_from_event(trip, event, profile) if create_activity else None

    # A *timed* imported event whose location became an activity keeps its own event-shaped link
    # (see _sync_activity_events/activity_to_event_body), rather than the trip-level link reusing
    # the same google_event_id.
    # The trip-level link always maps to an *all-day* body
    timed_import = activity is not None and activity.scheduled_at is not None
    TripCalendarLink.objects.create(
        trip=trip,
        profile=profile,
        google_calendar_id=account.calendar_id,
        google_event_id="" if timed_import else event_id,
        direction=CalendarSyncDirection.IMPORTED,
        last_synced=timezone.now(),
        auto_sync=auto_sync,
    )
    if timed_import:
        TripCalendarLink.objects.create(
            trip=trip,
            activity=activity,
            profile=profile,
            google_calendar_id=account.calendar_id,
            google_event_id=event_id,
            direction=CalendarSyncDirection.IMPORTED,
            last_synced=timezone.now(),
        )
    return trip


def _event_fingerprint(body: dict[str, Any], account: GoogleCalendarAccount) -> str:
    """Hash of an event body and the calendar it is written to, recorded on the link once Google accepts it."""
    payload = json.dumps({"calendar": account.calendar_id, "event": body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _trip_page_url(trip: Trip) -> str:
    """The trip page's absolute URL, the same in a request and in a background push, so both write the same body."""
    return absolute_url(reverse("trips.detail", kwargs={"trip_slug": trip.slug}))


def _hidden_activity_ids_for(trip: Trip, profile: Profile) -> set[int]:
    """Ids of *trip*'s activities whose location *profile* is not permitted to see.
    The same gate ``services.trips.trip_visibility`` applies on the activities panel, the trip map, and AI suggestions, run here so an export cannot become the one surface that ignores it.

    Args:
        trip: The trip being exported.
        profile: The profile whose calendar is being written.

    Returns:
        The hidden activity ids; empty when nothing is restricted."""
    activities = list(trip.activities.select_related("added_by"))
    return viewer_hidden_activity_ids(activities, profile)


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarExportResult:
    """Where one export attempt left a trip on one calendar.

    Attributes:
        trip_link: The trip-level link.
        events_total: Events the trip needs on the calendar: its all-day event and one per scheduled activity.
        events_synced: How many of those now match the trip, whether this attempt or an earlier one wrote them.
        activities_synced: How many of ``events_synced`` are activity events.
        written: Calendar writes this attempt made: creates, updates and deletes.
        complete: Every event matches the trip and every unscheduled activity's event is gone. False when the
            calendar budget, ours or Google's rate limit, ran out first; the trip-level link then owes a push, which
            ``tasks.requeue_pending_calendar_pushes`` queues.
    """

    trip_link: TripCalendarLink
    events_total: int
    events_synced: int
    activities_synced: int
    written: int
    complete: bool


class _TripUnlinkedError(Exception):
    """The trip was taken off this calendar (removed, or its member left) while an export of it ran."""


class _TripCalendarExport:
    """One attempt at mirroring a trip onto one calendar.

    An event whose link's fingerprint matches its new body is skipped, so an attempt the budget cut short is resumed,
    not restarted, by the next. A link's fingerprint is cleared before its event is written and set again once Google
    answers, so a write whose answer was lost is redone rather than trusted. A create carries an id derived from the
    trip, activity and profile, so a create whose answer was lost is met with 409 on the retry rather than making a
    second event.

    Attributes:
        account: The calendar written to.
        trip: The trip mirrored.
        written: Calendar writes made so far, readable after :meth:`run` raises.
    """

    def __init__(self, account: GoogleCalendarAccount, trip: Trip) -> None:
        self.account = account
        self.trip = trip
        self.written = 0
        self._gateway = GoogleCalendarGateway(account=account)
        self._trip_link_pk: int | None = None

    def run(self) -> CalendarExportResult:
        """Write whatever on the calendar does not yet match the trip, in trip-event-then-activity order.

        Returns:
            How far the attempt got.

        Raises:
            ValueError: When the trip has no dates to export.
            RateLimitExceededError: The budget, ours or Google's rate limit, ran out before the trip had a link to resume from.
            GoogleAuthExpiredError: When Google has rejected the stored grant.
            GatewayRequestError: When a calendar write fails for any other reason.
        """
        profile = self.account.profile
        trip_url = _trip_page_url(self.trip)
        hidden_activity_ids = _hidden_activity_ids_for(self.trip, profile)
        trip_link = TripCalendarLink.objects.trip_level_link(self.trip, profile)
        self._trip_link_pk = trip_link.pk if trip_link is not None else None
        activity_links = TripCalendarLink.objects.activity_links_by_activity_id(self.trip, profile)
        trip_body = trip_to_event_body(self.trip, trip_url=trip_url, hidden_activity_ids=hidden_activity_ids, owns_event=self._owns_event(trip_link, activity=None))

        # The trip's all-day event first, then each scheduled activity's, in TripActivity order.
        plan: list[tuple[dict[str, Any], TripCalendarLink | None, TripActivity | None]] = [(trip_body, trip_link, None)]
        for scheduled in self.trip.activities.filter(scheduled_at__isnull=False).select_related("trip", "location", "pin__location"):
            link = activity_links.get(scheduled.pk)
            body = activity_to_event_body(scheduled, trip_url=trip_url, hidden_activity_ids=hidden_activity_ids, owns_event=self._owns_event(link, activity=scheduled))
            if body is not None:
                plan.append((body, link, scheduled))
        scheduled_ids = {activity.pk for _body, _link, activity in plan if activity is not None}
        # Activities that lost their schedule since the last export: their events go.
        unscheduled = [link for activity_id, link in activity_links.items() if activity_id not in scheduled_ids]

        matched = [False] * len(plan)
        complete = True
        try:
            for index, (body, link, activity) in enumerate(plan):
                synced_link = self._sync_event(body, link, activity=activity)
                if activity is None and synced_link is not None:
                    trip_link = synced_link
                    self._trip_link_pk = synced_link.pk
                matched[index] = True
            for link in unscheduled:
                self._gateway.delete_event(link.google_event_id)
                link.delete()
                self.written += 1
        except RateLimitExceededError as exc:
            if trip_link is None:
                raise
            logger.info("Calendar budget ran out (%s) exporting trip %s to profile %s's calendar after %d writes; a push will finish it.", type(exc).__name__, self.trip.uuid, profile.pk, self.written)
            self._owe_push(trip_link)
            complete = False
            stopped_at = matched.index(False) if False in matched else len(plan)
            for index in range(stopped_at + 1, len(plan)):
                body, link, _activity = plan[index]
                matched[index] = self._matches(body, link)
        except _TripUnlinkedError:
            # Nothing is owed: the person took the trip off this calendar, and writing on would put events back.
            logger.info("Trip %s left profile %s's calendar during its export after %d writes; stopped.", self.trip.uuid, profile.pk, self.written)
            if trip_link is None:
                raise AssertionError("only a trip with a link can lose it") from None
            complete = False
        return CalendarExportResult(
            trip_link=trip_link,
            events_total=len(plan),
            events_synced=sum(matched),
            activities_synced=sum(matched[1:]),
            written=self.written,
            complete=complete,
        )

    def _owns_event(self, link: TripCalendarLink | None, *, activity: TripActivity | None) -> bool:
        """Whether UrbanLens made, or is about to make, the event *link* names, rather than linking one the user had.

        An import links the user's own event, and a location that event came with is not one UrbanLens can tell from
        one it wrote. An event created under :func:`trip_event_id` is UrbanLens's whichever way the link was made.
        """
        if link is None or link.direction == CalendarSyncDirection.EXPORTED or not link.google_event_id:
            return True
        return link.google_event_id == trip_event_id(self.trip, self.account.profile, activity)

    def _matches(self, body: dict[str, Any], link: TripCalendarLink | None) -> bool:
        """Whether *link* records that its event already holds *body* on this calendar."""
        return link is not None and bool(link.google_event_id) and link.event_fingerprint == _event_fingerprint(body, self.account)

    def _owe_push(self, trip_link: TripCalendarLink) -> None:
        """Leave the rest to ``tasks.requeue_pending_calendar_pushes``: mark the trip as owing a push, keeping an older mark.

        An attempt that wrote something resets the attempt count, so the cap counts only attempts that made no progress.
        """
        updates: dict[str, Any] = {"push_requested_at": Coalesce(F("push_requested_at"), Value(timezone.now(), output_field=DateTimeField()))}
        if self.written:
            updates["push_attempts"] = 0
        TripCalendarLink.objects.filter(pk=trip_link.pk).update(**updates)

    def _sync_event(self, body: dict[str, Any], link: TripCalendarLink | None, *, activity: TripActivity | None) -> TripCalendarLink | None:
        """Bring one event in line with *body*, unless its link shows it already is.

        Args:
            body: Event resource payload.
            link: The existing link for this trip/activity and profile, if any.
            activity: The activity mirrored, or None for the trip-level all-day event.

        Returns:
            The up-to-date link, or None when the activity was deleted during the export and its event is not wanted.

        Raises:
            _TripUnlinkedError: The trip was taken off this calendar during the export.
        """
        if link is not None and self._matches(body, link):
            return link
        if not self._claim(link):
            return None
        fingerprint = _event_fingerprint(body, self.account)
        event: dict[str, Any] | None = None
        if link is not None and link.google_event_id:
            try:
                event = self._gateway.update_event(link.google_event_id, body)
            except CalendarEventNotFoundError:
                logger.info("Calendar event %s for trip %s vanished; recreating.", link.google_event_id, self.trip.uuid)
        if event is None:
            event = self._create_event(body, activity=activity)
        else:
            self.written += 1
        return self._record_link(link, event_id=event["id"], fingerprint=fingerprint, activity=activity)

    def _claim(self, link: TripCalendarLink | None) -> bool:
        """Clear *link*'s fingerprint before its event is written, and make sure the trip is still on this calendar.

        Returns:
            False when *link* is gone but the trip's link is not: its activity was deleted during the export.

        Raises:
            _TripUnlinkedError: The trip's own link is gone.
        """
        if link is not None and link.pk is not None:
            if TripCalendarLink.objects.filter(pk=link.pk).update(event_fingerprint=""):
                link.event_fingerprint = ""
                return True
            if link.activity_id is None:
                raise _TripUnlinkedError
        elif self._trip_link_pk is None:
            # The trip's first export, writing its own event: there is no link to lose yet.
            return True
        if not TripCalendarLink.objects.filter(pk=self._trip_link_pk).exists():
            raise _TripUnlinkedError
        return link is None

    def _create_event(self, body: dict[str, Any], *, activity: TripActivity | None) -> dict[str, Any]:
        """Create the event under its deterministic id, taking over the event already holding that id.

        The id is taken when an earlier attempt's create reached Google but its answer did not, or when the event was
        deleted (Google keeps a deleted event's id). Either way it is this trip's event for this profile, so it is
        updated, and marked confirmed in case it was deleted.
        """
        event_id = trip_event_id(self.trip, self.account.profile, activity)
        try:
            event = self._gateway.create_event(body, event_id=event_id)
        except CalendarEventExistsError:
            try:
                event = self._gateway.update_event(event_id, {**body, "status": "confirmed"})
            except CalendarEventNotFoundError:
                # The id is held but cannot be written; let Google pick a fresh one.
                event = self._gateway.create_event(body)
        self.written += 1
        return event

    def _record_link(self, link: TripCalendarLink | None, *, event_id: str, fingerprint: str, activity: TripActivity | None) -> TripCalendarLink:
        """Persist what was just written, onto the existing link or a new one.

        A concurrent export of the same trip can create the link between this attempt's read and its insert; the
        insert then loses to the unique constraint and the winner's row is updated instead.
        """
        if link is None:
            link = TripCalendarLink(trip=self.trip, activity=activity, profile=self.account.profile, direction=CalendarSyncDirection.EXPORTED)
        link.google_calendar_id = self.account.calendar_id
        link.google_event_id = event_id
        link.event_fingerprint = fingerprint
        link.last_synced = timezone.now()
        fields = ["google_calendar_id", "google_event_id", "event_fingerprint", "last_synced", "updated"]
        if link.pk is not None:
            link.save(update_fields=fields)
            return link
        try:
            with transaction.atomic():
                link.save()
        except IntegrityError:
            existing = TripCalendarLink.objects.for_trip_and_profile(self.trip, self.account.profile).filter(activity=activity).first()
            if existing is None:
                raise
            for field in fields[:-1]:
                setattr(existing, field, getattr(link, field))
            existing.save(update_fields=fields)
            link = existing
        return link


def trip_event_id(trip: Trip, profile: Profile, activity: TripActivity | None) -> str:
    """The id a trip's (or one of its activities') event is created under on one profile's calendar.

    Derived from this site's URL, the trip's uuid, the profile and the activity, so every retry of the same create
    sends the same id, two profiles sharing one Google calendar get distinct events, and a copy of this database
    restored on another site cannot claim this site's events.

    Args:
        trip: The trip.
        profile: Whose calendar the event is on.
        activity: The activity the event mirrors, or None for the trip's all-day event.

    Returns:
        A Google event id (:func:`~urbanlens.dashboard.services.apis.calendar.google.client_event_id`).
    """
    return client_event_id(absolute_url(), trip.uuid, profile.pk, activity.pk if activity is not None else "trip")


def export_progress_message(result: CalendarExportResult) -> str:
    """How far an unfinished export got, for the person who started it.

    Args:
        result: An export that did not complete.

    Returns:
        A sentence giving how many of the trip's events are on the calendar.
    """
    return f"{result.events_synced} of {result.events_total} events are on your Google Calendar. The rest will follow automatically."


def export_trip_to_calendar(account: GoogleCalendarAccount, trip: Trip) -> CalendarExportResult:
    """Mirror a trip (all-day event) and its scheduled activities (timed events) to the user's calendar.

    Writes only what does not already match. When the calendar budget runs out partway, the result is incomplete
    and the trip-level link owes a push, which ``tasks.requeue_pending_calendar_pushes`` delivers.

    Args:
        account: The user's connected calendar account.
        trip: The trip to export.

    Returns:
        Where the attempt left the trip on the calendar.

    Raises:
        ValueError: When the trip has no dates to export.
        RateLimitExceededError: The budget, ours or Google's rate limit, ran out before anything of the trip was on the calendar.
        GoogleAuthExpiredError: When Google has rejected the stored grant and the connection must be re-established.
        GatewayRequestError: When a calendar write fails."""
    return _TripCalendarExport(account, trip).run()


def trip_calendar_status(trip: Trip, profile: Profile) -> dict[str, Any]:
    """Describe one profile's calendar mirroring of one trip.
    It is a service rather than a view helper because three surfaces answer the same question - the trip detail bundle, the auto-sync toggle, and the export/unexport pair - and a second copy would be the one that forgets a field.

    Args:
        trip: The trip in question.
        profile: The profile whose calendar is being described.

    Returns:
        Dict with ``connected``, ``linked``, ``auto_sync``, ``last_synced`` and ``account_email`` keys."""
    account = GoogleCalendarAccount.objects.get_for_profile(profile)
    link = TripCalendarLink.objects.trip_level_link(trip, profile) if account else None
    return {
        "connected": account is not None,
        "linked": link is not None,
        "auto_sync": bool(link and link.auto_sync),
        "last_synced": link.last_synced if link else None,
        "account_email": account.google_email if account else None,
    }


def remove_trip_from_calendar(account: GoogleCalendarAccount, trip: Trip) -> bool:
    """Delete the calendar events linked to a trip for this user and drop the links.

    Args:
        account: The user's connected calendar account.
        trip: The trip whose events should be removed.

    Returns:
        True when at least one link existed and was removed.

    Raises:
        GatewayRequestError: When a calendar delete fails."""
    links = list(TripCalendarLink.objects.filter(trip=trip, profile=account.profile))
    if not links:
        return False
    gateway = GoogleCalendarGateway(account=account)
    for link in links:
        # A trip-level link created for a timed import starts with a blank google_event_id (see
        # import_events_as_trips) until the first export/auto-sync push actually creates its all-day
        # mirror - there's nothing on Google's side to delete yet.
        if link.google_event_id:
            gateway.delete_event(link.google_event_id)
        link.delete()
    return True


def disconnect_member_calendar_sync(trip: Trip, profile: Profile) -> None:
    """Stop syncing a trip to one profile's calendar when they leave or are removed.

    Args:
        trip: The trip the profile is leaving/being removed from.
        profile: The departing profile."""
    links = list(TripCalendarLink.objects.filter(trip=trip, profile=profile))
    if not links:
        return

    account = GoogleCalendarAccount.objects.get_for_profile(profile)
    if account is not None:
        gateway = GoogleCalendarGateway(account=account)
        for link in links:
            # See remove_trip_from_calendar: a trip-level link from a timed
            # import can still be carrying a blank google_event_id if no
            # export/auto-sync push has happened yet.
            if not link.google_event_id:
                continue
            try:
                gateway.delete_event(link.google_event_id)
            except GatewayRequestError:
                logger.warning(
                    "Could not delete calendar event %s for departing trip member %s; dropping the sync link anyway.",
                    link.google_event_id,
                    profile.pk,
                    exc_info=True,
                )

    TripCalendarLink.objects.filter(trip=trip, profile=profile).delete()


def push_auto_synced_trip_changes(trip: Trip) -> int:
    """Push a trip's current state to every calendar it is set to auto-sync with, or that an export left unfinished.
    Only trip-level links are pushed, and only one way (UrbanLens to Google): edits made on the Google Calendar side
    are never pulled back in. A link without ``auto_sync`` is pushed only while it owes a push, which an export the
    calendar budget cut short leaves behind.

    A link's ``push_requested_at`` is cleared only by a push that finished and that still finds the value read before
    it, so a change made during the push stays owed. A push the budget cut short, or that failed, leaves the request
    for ``tasks.requeue_pending_calendar_pushes``, and counts an attempt only if it wrote nothing: one that made
    progress resets the count, so the cap drops a request only after pushes that went nowhere. A trip with no dates,
    or a grant Google revoked, cannot be pushed until something changes, so it settles the request instead.

    Args:
        trip: The trip whose linked calendar events should be refreshed.

    Returns:
        The number of calendars the trip was fully pushed to."""
    links = TripCalendarLink.objects.filter(trip=trip, activity__isnull=True).filter(Q(auto_sync=True) | Q(push_requested_at__isnull=False)).select_related("profile")
    synced = 0
    for link in links:
        requested = link.push_requested_at
        account = GoogleCalendarAccount.objects.get_for_profile(link.profile)
        if account is None:
            _settle_push_request(link, requested)
            continue
        export = _TripCalendarExport(account, trip)
        try:
            result = export.run()
        except (GoogleAuthExpiredError, ValueError):
            logger.warning("Auto-sync of trip %s to profile %s's calendar cannot be pushed until it changes.", trip.uuid, link.profile_id, exc_info=True)
            _settle_push_request(link, requested)
            continue
        except GatewayRequestError:
            logger.warning("Auto-sync of trip %s to profile %s's calendar failed after %d writes.", trip.uuid, link.profile_id, export.written, exc_info=True)
            _count_push_attempt(link, made_progress=export.written > 0)
            continue
        if not result.complete:
            _count_push_attempt(link, made_progress=result.written > 0)
            continue
        _settle_push_request(link, requested)
        synced += 1
    return synced


def _count_push_attempt(link: TripCalendarLink, *, made_progress: bool) -> None:
    """Record a push that left the request owed: one that wrote nothing counts toward the cap, one that wrote resets it."""
    TripCalendarLink.objects.filter(pk=link.pk).update(push_attempts=0 if made_progress else F("push_attempts") + 1)


def _settle_push_request(link: TripCalendarLink, requested: datetime.datetime | None) -> None:
    if requested is not None:
        TripCalendarLink.objects.filter(pk=link.pk, push_requested_at=requested).update(push_requested_at=None, push_attempts=0)
