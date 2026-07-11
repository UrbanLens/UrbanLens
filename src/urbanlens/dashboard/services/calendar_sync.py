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
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import (
    ACTIVITY_ID_EVENT_PROPERTY,
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

# Length of the calendar event created for an activity that has a start time
# but no explicit end time.
DEFAULT_ACTIVITY_EVENT_DURATION = datetime.timedelta(hours=2)


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


def _activity_location_string(activity: TripActivity) -> str | None:
    """Human-readable location for an activity's calendar event.

    Respects ``location_hidden`` (secret locations are never exported).
    Prefers the linked place's address, falling back to coordinates.

    Args:
        activity: The TripActivity to describe.

    Returns:
        A location string for the event, or None when nothing shareable exists.
    """
    if activity.location_hidden:
        return None
    location = activity.location or (activity.pin.location if activity.pin else None)
    if location is not None and location.address:
        return location.address
    lat = activity.lat_override if activity.lat_override is not None else (float(location.latitude) if location else None)
    lng = activity.lng_override if activity.lng_override is not None else (float(location.longitude) if location else None)
    if lat is not None and lng is not None:
        return f"{lat:.6f}, {lng:.6f}"
    return None


def activity_to_event_body(activity: TripActivity, *, trip_url: str | None = None) -> dict[str, Any] | None:
    """Convert one scheduled trip activity into a timed calendar event payload.

    Activities without a scheduled start cannot be placed on a calendar and
    yield None. The end time is the activity's ``scheduled_end`` when it is
    after the start, else start + :data:`DEFAULT_ACTIVITY_EVENT_DURATION`.

    Args:
        activity: The TripActivity to export (with ``trip`` loaded).
        trip_url: Optional absolute URL of the trip page to append to the
            event description.

    Returns:
        Event resource payload, or None when the activity is unscheduled.
    """
    if activity.scheduled_at is None:
        return None
    start = activity.scheduled_at
    end = activity.scheduled_end
    if end is None or end <= start:
        end = start + DEFAULT_ACTIVITY_EVENT_DURATION

    description = (activity.notes or "").strip()
    if trip_url:
        description = f"{description}\n\n{trip_url}".strip()

    body: dict[str, Any] = {
        "summary": f"{activity.trip.name}: {activity.effective_title}",
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
    location_string = _activity_location_string(activity)
    if location_string:
        body["location"] = location_string
    return body


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


def _parse_event_datetime(part: dict[str, Any] | None) -> datetime.datetime | None:
    """Parse the timestamp from one side of a *timed* event's start/end structure.

    All-day events (``{"date": ...}``) yield None - only ``dateTime`` values
    carry a usable time of day.

    Args:
        part: The event's ``start`` or ``end`` dict.

    Returns:
        The parsed datetime, or None for all-day/missing/unparsable values.
    """
    raw = (part or {}).get("dateTime")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def match_event_attendees(profile: Profile, event: dict[str, Any]) -> tuple[list[Profile], list[str]]:
    """Split an event's attendees into invitable friends and everyone else.

    An attendee is invitable when their email resolves to an UrbanLens account
    that is an accepted friend of ``profile``. The importer themselves is
    excluded. Non-friends and unknown addresses are returned only as display
    labels - no account information is revealed beyond what the importer's own
    calendar already shows.

    Args:
        profile: The importing user's profile.
        event: Event resource dict from the Calendar API.

    Returns:
        Tuple of (friend profiles that can be invited, display labels for the
        remaining attendees).
    """
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.services.email_normalization import find_user_by_email, normalize_email

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
        user = find_user_by_email(email)
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
    Entries that cannot be imported carry a ``skip_reason`` instead of import
    details.

    Args:
        account: The user's connected calendar account.
        event_ids: Google event ids selected on page one.

    Returns:
        List of preview dicts with ``event_id``, ``summary``, ``trip_kwargs``,
        ``location``, ``scheduled_at``/``scheduled_end`` (for timed events),
        ``friends``, ``other_attendees``, and ``skip_reason`` keys.

    Raises:
        GatewayRequestError: When the calendar cannot be read.
    """
    gateway = GoogleCalendarGateway(account=account)
    profile = account.profile
    previews: list[dict[str, Any]] = []

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

        if TripCalendarLink.objects.filter(profile=profile, google_event_id=event_id).exists():
            entry["skip_reason"] = "Already linked to a trip."
            continue
        try:
            event = gateway.get_event(event_id)
        except CalendarEventNotFoundError:
            entry["skip_reason"] = "This event no longer exists on your calendar."
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

    The raw location string becomes the activity title (it is free-form text
    on Google's side - no geocoding is attempted). Timed events also carry
    their start/end over to the activity schedule.

    Args:
        trip: The freshly imported trip.
        event: Event resource dict the trip was created from.
        profile: The importing user's profile.

    Returns:
        The created activity, or None when the event has no location.
    """
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

    Every id is re-validated server side: only accepted friends of the
    importer are ever added, regardless of what the client submitted.

    Args:
        trip: The freshly imported trip.
        importer: The importing user's profile.
        profile_ids: Profile ids the importer confirmed on the review page.
        skipped: Mutable list human-readable skip reasons are appended to.

    Returns:
        The number of members actually added.
    """
    from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if not profile_ids:
        return 0

    from django.urls import reverse

    max_members = SiteSettings.get_current().max_trip_members
    invited = 0
    for profile_id in profile_ids:
        invitee = ProfileModel.objects.filter(pk=profile_id).select_related("user").first()
        if invitee is None or invitee.pk == importer.pk:
            continue
        if not ProfileModel.are_friends(importer, invitee):
            skipped.append(f"{invitee.username} was not invited because you are not friends on UrbanLens.")
            continue
        if trip.profiles.count() >= max_members:
            skipped.append(f'"{trip.name}" is full ({max_members} members maximum); some invitations were not sent.')
            break
        _membership, created = TripMembership.objects.get_or_create(trip=trip, profile=invitee)
        if created:
            NotificationLog.objects.create(
                profile=invitee,
                status=Status.UNREAD,
                importance=Importance.MEDIUM,
                notification_type=NotificationType.ADDED_TO_TRIP,
                title="Added to a trip",
                message=f'{importer.username} added you to the trip "{trip.name}".',
                url=reverse("trips.detail", kwargs={"trip_uuid": trip.uuid}),
            )
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


def import_events_as_trips(account: GoogleCalendarAccount, selections: list[str | dict[str, Any]]) -> tuple[list[Trip], list[str], int]:
    """Create trips from the given calendar events on the user's calendar.

    Events are re-fetched individually so only data Google actually returns
    is trusted (the client submits ids, never event content). Events already
    linked to a trip for this profile, events exported from UrbanLens, and
    unparsable events are skipped with a reason.

    Each selection may carry per-event options confirmed on the review page:
    whether to create an activity from the event's location, and which friend
    profiles to invite as trip members. Invitees are re-validated - only
    accepted friends of the importer are ever added.

    Args:
        account: The user's connected calendar account.
        selections: Either bare Google event ids, or dicts with ``event_id``,
            ``create_activity`` (bool, default True), and ``invite_profile_ids``
            (list of ints, default empty) keys.

    Returns:
        Tuple of (created trips, human-readable skip reasons, number of
        participants invited).
    """
    gateway = GoogleCalendarGateway(account=account)
    profile = account.profile
    created: list[Trip] = []
    skipped: list[str] = []
    invited_total = 0

    for raw_selection in selections:
        selection: dict[str, Any] = {"event_id": raw_selection} if isinstance(raw_selection, str) else raw_selection
        event_id = selection.get("event_id") or ""
        if not event_id:
            continue

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
        if selection.get("create_activity", True):
            _create_activity_from_event(trip, event, profile)
        invited_total += _invite_participants(trip, profile, list(selection.get("invite_profile_ids") or []), skipped)
        created.append(trip)

    return created, skipped, invited_total


def _upsert_event_link(
    gateway: GoogleCalendarGateway,
    account: GoogleCalendarAccount,
    body: dict[str, Any],
    link: TripCalendarLink | None,
    *,
    trip: Trip,
    activity: TripActivity | None = None,
) -> TripCalendarLink:
    """Create or update one calendar event and persist its link row.

    An existing link updates its event in place; if that event was deleted on
    the Google side, a fresh one is created and the link is repointed.

    Args:
        gateway: Authenticated calendar gateway.
        account: The user's connected calendar account.
        body: Event resource payload to write.
        link: Existing link row for this trip/activity+profile, if any.
        trip: The trip the event belongs to.
        activity: The activity mirrored by this event, or None for the
            trip-level all-day event.

    Returns:
        The up-to-date TripCalendarLink row.

    Raises:
        GatewayRequestError: When the calendar write fails.
    """
    event: dict[str, Any] | None = None
    if link and link.google_event_id:
        try:
            event = gateway.update_event(link.google_event_id, body)
        except CalendarEventNotFoundError:
            logger.info("Calendar event %s for trip %s vanished; recreating.", link.google_event_id, trip.uuid)

    if event is None:
        event = gateway.create_event(body)

    if link is None:
        link = TripCalendarLink(trip=trip, activity=activity, profile=account.profile, direction=CalendarSyncDirection.EXPORTED)
    link.google_calendar_id = account.calendar_id
    link.google_event_id = event["id"]
    link.last_synced = timezone.now()
    link.save()
    return link


def _sync_activity_events(
    gateway: GoogleCalendarGateway,
    account: GoogleCalendarAccount,
    trip: Trip,
    *,
    trip_url: str | None = None,
) -> int:
    """Mirror every scheduled activity of a trip as a timed event on the user's calendar.

    Creates/updates one event per activity with a start time, and deletes
    events for activities that were unscheduled since the last export
    (activities deleted outright cascade their link rows away, so their
    events are cleaned up by the caller's full-removal path or simply left
    to the user - Google shows them as normal events).

    Args:
        gateway: Authenticated calendar gateway.
        account: The user's connected calendar account.
        trip: The trip whose activities to mirror.
        trip_url: Optional absolute trip URL for event descriptions.

    Returns:
        The number of activity events created or updated.

    Raises:
        GatewayRequestError: When a calendar write fails.
    """
    profile = account.profile
    activity_links = {link.activity_id: link for link in TripCalendarLink.objects.filter(trip=trip, profile=profile, activity__isnull=False)}

    exported = 0
    scheduled_ids: set[int] = set()
    for activity in trip.activities.filter(scheduled_at__isnull=False).select_related("trip", "location", "pin__location"):
        body = activity_to_event_body(activity, trip_url=trip_url)
        if body is None:
            continue
        _upsert_event_link(gateway, account, body, activity_links.get(activity.pk), trip=trip, activity=activity)
        scheduled_ids.add(activity.pk)
        exported += 1

    # Activities that lost their schedule since the last export: remove their events.
    for activity_id, stale_link in activity_links.items():
        if activity_id not in scheduled_ids:
            gateway.delete_event(stale_link.google_event_id)
            stale_link.delete()

    return exported


def export_trip_to_calendar(account: GoogleCalendarAccount, trip: Trip, *, trip_url: str | None = None) -> tuple[TripCalendarLink, int]:
    """Mirror a trip (all-day event) and its scheduled activities (timed events) to the user's calendar.

    A trip already linked for this profile updates its existing events; events
    deleted on the Google side are recreated and their links repointed.

    Args:
        account: The user's connected calendar account.
        trip: The trip to export.
        trip_url: Optional absolute trip URL for the event descriptions.

    Returns:
        Tuple of (the trip-level TripCalendarLink row, number of activity
        events created or updated).

    Raises:
        ValueError: When the trip has no dates to export.
        GatewayRequestError: When a calendar write fails.
    """
    gateway = GoogleCalendarGateway(account=account)
    body = trip_to_event_body(trip, trip_url=trip_url)
    profile = account.profile

    trip_link = TripCalendarLink.objects.filter(trip=trip, profile=profile, activity__isnull=True).first()
    trip_link = _upsert_event_link(gateway, account, body, trip_link, trip=trip)
    activity_count = _sync_activity_events(gateway, account, trip, trip_url=trip_url)
    return trip_link, activity_count


def remove_trip_from_calendar(account: GoogleCalendarAccount, trip: Trip) -> bool:
    """Delete the calendar events linked to a trip for this user and drop the links.

    Removes the trip-level all-day event and every per-activity event that was
    exported for this profile. Events already deleted on the Google side are
    treated as removed.

    Args:
        account: The user's connected calendar account.
        trip: The trip whose events should be removed.

    Returns:
        True when at least one link existed and was removed.

    Raises:
        GatewayRequestError: When a calendar delete fails.
    """
    links = list(TripCalendarLink.objects.filter(trip=trip, profile=account.profile))
    if not links:
        return False
    gateway = GoogleCalendarGateway(account=account)
    for link in links:
        gateway.delete_event(link.google_event_id)
        link.delete()
    return True
