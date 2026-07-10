"""Trip ↔ Google Calendar event conversion and sync orchestration.

Everything here operates on the *current user's own* calendar via their
:class:`~urbanlens.dashboard.models.calendar_sync.GoogleCalendarAccount`.
Pure mapping helpers (:func:`trip_to_event_body`, :func:`event_to_trip_kwargs`)
are kept free of I/O so they can be property-tested; the ``import_*`` /
``export_*`` functions do the API calls and bookkeeping.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, TripCalendarLink
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import (
    TRIP_UUID_EVENT_PROPERTY,
    CalendarEventNotFoundError,
    GoogleCalendarGateway,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

# How far ahead the import dialog looks for events.
IMPORT_WINDOW_DAYS = 365
_MAX_TRIP_NAME_LENGTH = 255


def trip_to_event_body(trip: Trip, *, trip_url: str | None = None) -> dict[str, Any]:
    """Convert a trip into a Google Calendar all-day event payload.

    Trips carry dates (not times), so they map to all-day events. Google's
    all-day ``end.date`` is exclusive, so one day is added to the trip's
    inclusive end date.

    Args:
        trip: The trip to export. Must have an effective start date.
        trip_url: Optional absolute URL of the trip page to append to the
            event description.

    Returns:
        Event resource payload for the Calendar API.

    Raises:
        ValueError: When the trip has no start date (no dates and no
            scheduled activities).
    """
    start = trip.effective_start_date
    if start is None:
        raise ValueError("Trip has no start date or scheduled activities - set dates before exporting.")
    end = trip.effective_end_date or start
    end = max(end, start)

    description = (trip.description or "").strip()
    if trip_url:
        description = f"{description}\n\n{trip_url}".strip()

    return {
        "summary": trip.name,
        "description": description,
        "start": {"date": start.isoformat()},
        "end": {"date": (end + datetime.timedelta(days=1)).isoformat()},
        "extendedProperties": {"private": {TRIP_UUID_EVENT_PROPERTY: str(trip.uuid)}},
    }


def _parse_event_date(part: dict[str, Any] | None) -> tuple[datetime.date | None, bool]:
    """Parse the date from one side of an event's start/end structure.

    Args:
        part: The event's ``start`` or ``end`` dict (``{"date": ...}`` for
            all-day events, ``{"dateTime": ...}`` for timed ones).

    Returns:
        Tuple of (parsed date or None, whether it was an all-day ``date``).
    """
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

    All-day events have an exclusive end date, which is converted back to the
    trip's inclusive end date. Timed events use the date component of their
    start/end. Cancelled events and events without a parsable start are
    rejected.

    Args:
        event: Event resource dict from the Calendar API.

    Returns:
        Kwargs for creating a Trip (name, description, start_date, end_date),
        or None when the event cannot become a trip.
    """
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


def event_originated_from_urbanlens(event: dict[str, Any]) -> bool:
    """Whether an event was created by an UrbanLens trip export.

    Args:
        event: Event resource dict.

    Returns:
        True when the event carries the private trip-UUID marker property.
    """
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return bool(private.get(TRIP_UUID_EVENT_PROPERTY))


def list_importable_events(account: GoogleCalendarAccount) -> list[dict[str, Any]]:
    """Fetch upcoming events from the user's calendar, annotated for the import dialog.

    Each returned dict wraps the raw event with the parsed trip kwargs and
    flags explaining why an event may not be selectable (already imported,
    exported from UrbanLens, or unparsable).

    Args:
        account: The user's connected calendar account.

    Returns:
        List of ``{"event", "trip_kwargs", "already_linked", "from_urbanlens"}``
        dicts in calendar order.

    Raises:
        GatewayRequestError: When the calendar cannot be read.
    """
    gateway = GoogleCalendarGateway(account=account)
    now = timezone.now()
    events = gateway.list_events(
        time_min=now - datetime.timedelta(days=1),
        time_max=now + datetime.timedelta(days=IMPORT_WINDOW_DAYS),
    )

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
    return results


def import_events_as_trips(account: GoogleCalendarAccount, event_ids: list[str]) -> tuple[list[Trip], list[str]]:
    """Create trips from the given calendar events on the user's calendar.

    Events are re-fetched individually so only data Google actually returns
    is trusted (the client submits ids, never event content). Events already
    linked to a trip for this profile, events exported from UrbanLens, and
    unparsable events are skipped with a reason.

    Args:
        account: The user's connected calendar account.
        event_ids: Google event ids selected in the import dialog.

    Returns:
        Tuple of (created trips, human-readable skip reasons).
    """
    gateway = GoogleCalendarGateway(account=account)
    profile = account.profile
    created: list[Trip] = []
    skipped: list[str] = []

    for event_id in event_ids:
        if TripCalendarLink.objects.filter(profile=profile, google_event_id=event_id).exists():
            skipped.append("An event was skipped because it is already linked to a trip.")
            continue

        try:
            event = gateway.get_event(event_id)
        except CalendarEventNotFoundError:
            skipped.append("An event was skipped because it no longer exists on your calendar.")
            continue

        if event_originated_from_urbanlens(event):
            skipped.append(f'"{event.get("summary") or "Untitled"}" was skipped because it was exported from an UrbanLens trip.')
            continue

        kwargs = event_to_trip_kwargs(event)
        if kwargs is None:
            skipped.append(f'"{event.get("summary") or "Untitled"}" could not be converted to a trip.')
            continue

        trip = Trip.objects.create(creator=profile, **kwargs)
        TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"rsvp": TripMembership.RSVP_YES})
        TripCalendarLink.objects.create(
            trip=trip,
            profile=profile,
            google_calendar_id=account.calendar_id,
            google_event_id=event_id,
            direction=CalendarSyncDirection.IMPORTED,
            last_synced=timezone.now(),
        )
        created.append(trip)

    return created, skipped


def export_trip_to_calendar(account: GoogleCalendarAccount, trip: Trip, *, trip_url: str | None = None) -> TripCalendarLink:
    """Create or update the calendar event mirroring a trip on the user's calendar.

    A trip already linked for this profile updates its existing event; if
    that event was deleted on the Google side, a fresh one is created and the
    link is repointed.

    Args:
        account: The user's connected calendar account.
        trip: The trip to export.
        trip_url: Optional absolute trip URL for the event description.

    Returns:
        The up-to-date TripCalendarLink row.

    Raises:
        ValueError: When the trip has no dates to export.
        GatewayRequestError: When the calendar write fails.
    """
    gateway = GoogleCalendarGateway(account=account)
    body = trip_to_event_body(trip, trip_url=trip_url)
    profile = account.profile

    link = TripCalendarLink.objects.filter(trip=trip, profile=profile).first()
    event: dict[str, Any] | None = None
    if link and link.google_event_id:
        try:
            event = gateway.update_event(link.google_event_id, body)
        except CalendarEventNotFoundError:
            logger.info("Calendar event %s for trip %s vanished; recreating.", link.google_event_id, trip.uuid)

    if event is None:
        event = gateway.create_event(body)

    if link is None:
        link = TripCalendarLink(trip=trip, profile=profile, direction=CalendarSyncDirection.EXPORTED)
    link.google_calendar_id = account.calendar_id
    link.google_event_id = event["id"]
    link.last_synced = timezone.now()
    link.save()
    return link


def remove_trip_from_calendar(account: GoogleCalendarAccount, trip: Trip) -> bool:
    """Delete the calendar event linked to a trip for this user and drop the link.

    Args:
        account: The user's connected calendar account.
        trip: The trip whose event should be removed.

    Returns:
        True when a link existed and was removed.

    Raises:
        GatewayRequestError: When the calendar delete fails.
    """
    link = TripCalendarLink.objects.filter(trip=trip, profile=account.profile).first()
    if link is None:
        return False
    gateway = GoogleCalendarGateway(account=account)
    gateway.delete_event(link.google_event_id)
    link.delete()
    return True
