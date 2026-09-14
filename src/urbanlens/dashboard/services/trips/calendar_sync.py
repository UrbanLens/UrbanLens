"""Trip ↔ Google Calendar event conversion and sync orchestration.
Pure mapping helpers (:func:`trip_to_event_body`, :func:`event_to_trip_kwargs`) are kept free of I/O so they can be property-tested; the ``import_*`` / ``export_*`` functions do the API calls and bookkeeping."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import (
    ACTIVITY_ID_EVENT_PROPERTY,
    TRIP_UUID_EVENT_PROPERTY,
    CalendarEventNotFoundError,
    GoogleCalendarGateway,
)
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

# How far ahead the import dialog looks for events.
IMPORT_WINDOW_DAYS = 365
_MAX_TRIP_NAME_LENGTH = 255

# Length of the calendar event created for an activity that has a start time
# but no explicit end time.
DEFAULT_ACTIVITY_EVENT_DURATION = datetime.timedelta(hours=2)


def trip_to_event_body(trip: Trip, *, trip_url: str | None = None, hidden_activity_ids: Collection[int] | None = None) -> dict[str, Any]:
    """Convert a trip into a Google Calendar all-day event payload.
    Trips carry dates (not times), so they map to all-day events.

    Args:
        trip: The trip to export.
        trip_url: Optional absolute URL of the trip page to append to the event description.
        hidden_activity_ids: Ids of activities whose location the *exporting* viewer may not see, from :func:`~urbanlens.dashboard.services.trips.trip_visibility.viewer_hidden_activity_ids`.

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
    location_string = _trip_location_string(trip, hidden_activity_ids=hidden_activity_ids)
    if location_string:
        body["location"] = location_string
    return body


def _activity_location_string(activity: TripActivity, *, hidden_activity_ids: Collection[int] | None = None) -> str | None:
    """Human-readable location for an activity's calendar event.

    Args:
        activity: The TripActivity to describe.
        hidden_activity_ids: Ids of activities whose location the exporting viewer may not see.

    Returns:
        A location string for the event, or None when nothing shareable exists."""
    if activity.location_hidden:
        return None
    if hidden_activity_ids is not None and activity.pk in hidden_activity_ids:
        return None
    location = activity.location or (activity.pin.location if activity.pin else None)
    if location is not None and location.address:
        return location.address
    lat = activity.lat_override if activity.lat_override is not None else (float(location.latitude) if location else None)
    lng = activity.lng_override if activity.lng_override is not None else (float(location.longitude) if location else None)
    if lat is not None and lng is not None:
        return f"{lat:.6f}, {lng:.6f}"
    return None


def _trip_location_string(trip: Trip, *, hidden_activity_ids: Collection[int] | None = None) -> str | None:
    """Human-readable location for the trip-level calendar event.
    Uses the trip's first activity (by schedule, then manual order) that has a shareable location, so the exported all-day event points at where the trip starts.

    Args:
        trip: The trip being exported.
        hidden_activity_ids: Ids of activities whose location the exporting viewer may not see - see :func:`_activity_location_string`.

    Returns:
        A location string for the event, or None when no activity has one."""
    if trip.pk is None:
        # Unsaved trips (e.g. pure-mapping property tests) have no activities.
        return None
    for activity in trip.activities.select_related("location", "pin__location"):
        location_string = _activity_location_string(activity, hidden_activity_ids=hidden_activity_ids)
        if location_string:
            return location_string
    return None


def activity_to_event_body(activity: TripActivity, *, trip_url: str | None = None, hidden_activity_ids: Collection[int] | None = None) -> dict[str, Any] | None:
    """Convert one scheduled trip activity into a timed calendar event payload.
    Activities without a scheduled start cannot be placed on a calendar and yield None.

    Args:
        activity: The TripActivity to export (with ``trip`` loaded).
        trip_url: Optional absolute URL of the trip page to append to the event description.
        hidden_activity_ids: Ids of activities whose location the exporting viewer may not see - see :func:`_activity_location_string`.

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
    location_string = _activity_location_string(activity, hidden_activity_ids=hidden_activity_ids)
    if location_string:
        body["location"] = location_string
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
        The parsed datetime, or None for all-day/missing/unparsable values."""
    raw = (part or {}).get("dateTime")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def match_event_attendees(profile: Profile, event: dict[str, Any]) -> tuple[list[Profile], list[str]]:
    """Split an event's attendees into invitable friends and everyone else.
    Non-friends and unknown addresses are returned only as display labels - no account information is revealed beyond what the importer's own calendar already shows.

    Args:
        profile: The importing user's profile.
        event: Event resource dict from the Calendar API.

    Returns:
        Tuple of (friend profiles that can be invited, display labels for the remaining attendees)."""
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.services.auth.email_normalization import find_user_by_email, normalize_email

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

    Args:
        account: The user's connected calendar account.
        event_ids: Google event ids selected on page one.

    Returns:
        List of preview dicts with ``event_id``, ``summary``, ``trip_kwargs``, ``location``, ``scheduled_at``/``scheduled_end`` (for timed events), ``friends``, ``other_attendees``, and ``skip_reason`` keys.

    Raises:
        GatewayRequestError: When the calendar cannot be read."""
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

        if TripCalendarLink.objects.already_linked(profile, event_id):
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
    from urbanlens.dashboard.services.trips.trip_membership import notify_added_to_trip

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
        if trip.profiles.count() >= max_members:
            skipped.append(f'"{trip.name}" is full ({max_members} members maximum); some invitations were not sent.')
            break
        _membership, created = TripMembership.objects.get_or_create(trip=trip, profile=invitee, defaults={"status": TripMembership.STATUS_INVITED})
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


def list_importable_events(account: GoogleCalendarAccount) -> list[dict[str, Any]]:
    """Fetch upcoming events from the user's calendar, annotated for the import dialog.

    Args:
        account: The user's connected calendar account.

    Returns:
        List of ``{"event", "trip_kwargs", "already_linked", "from_urbanlens"}`` dicts in calendar order.

    Raises:
        GatewayRequestError: When the calendar cannot be read."""
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


def import_events_as_trips(account: GoogleCalendarAccount, selections: Sequence[str | dict[str, Any]]) -> tuple[list[Trip], list[str], int]:
    """Create trips from the given calendar events on the user's calendar.
    Events are re-fetched individually so only data Google actually returns is trusted (the client submits ids, never event content).

    Args:
        account: The user's connected calendar account.
        selections: Either bare Google event ids, or dicts with ``event_id``, ``create_activity`` (bool, default True), ``invite_profile_ids`` (list of ints, default empty), and ``auto_sync`` (bool, default False) keys.

    Returns:
        Tuple of (created trips, human-readable skip reasons, number of participants invited)."""
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

        if TripCalendarLink.objects.already_linked(profile, event_id):
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

    Args:
        gateway: Authenticated calendar gateway.
        account: The user's connected calendar account.
        body: Event resource payload to write.
        link: Existing link row for this trip/activity+profile, if any.
        trip: The trip the event belongs to.
        activity: The activity mirrored by this event, or None for the trip-level all-day event.

    Returns:
        The up-to-date TripCalendarLink row.

    Raises:
        GatewayRequestError: When the calendar write fails."""
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
    hidden_activity_ids: Collection[int] | None = None,
) -> int:
    """Mirror every scheduled activity of a trip as a timed event on the user's calendar.

    Args:
        gateway: Authenticated calendar gateway.
        account: The user's connected calendar account.
        trip: The trip whose activities to mirror.
        trip_url: Optional absolute trip URL for event descriptions.
        hidden_activity_ids: Ids of activities whose location this account's owner may not see - see :func:`_activity_location_string`.

    Returns:
        The number of activity events created or updated.

    Raises:
        GatewayRequestError: When a calendar write fails."""
    profile = account.profile
    activity_links = TripCalendarLink.objects.activity_links_by_activity_id(trip, profile)

    exported = 0
    scheduled_ids: set[int] = set()
    for activity in trip.activities.filter(scheduled_at__isnull=False).select_related("trip", "location", "pin__location"):
        body = activity_to_event_body(activity, trip_url=trip_url, hidden_activity_ids=hidden_activity_ids)
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


def _hidden_activity_ids_for(trip: Trip, profile: Profile) -> set[int]:
    """Ids of *trip*'s activities whose location *profile* is not permitted to see.
    The same gate ``services.trips.trip_visibility`` applies on the activities panel, the trip map, and AI suggestions, run here so an export cannot become the one surface that ignores it.

    Args:
        trip: The trip being exported.
        profile: The profile whose calendar is being written.

    Returns:
        The hidden activity ids; empty when nothing is restricted."""
    from urbanlens.dashboard.services.trips.trip_visibility import viewer_hidden_activity_ids

    activities = list(trip.activities.select_related("added_by"))
    return viewer_hidden_activity_ids(activities, profile)


def export_trip_to_calendar(account: GoogleCalendarAccount, trip: Trip, *, trip_url: str | None = None) -> tuple[TripCalendarLink, int]:
    """Mirror a trip (all-day event) and its scheduled activities (timed events) to the user's calendar.

    Args:
        account: The user's connected calendar account.
        trip: The trip to export.
        trip_url: Optional absolute trip URL for the event descriptions.

    Returns:
        Tuple of (the trip-level TripCalendarLink row, number of activity events created or updated).

    Raises:
        ValueError: When the trip has no dates to export.
        GoogleAuthExpiredError: When Google has rejected the stored grant and the connection must be re-established.
        GatewayRequestError: When a calendar write fails."""
    gateway = GoogleCalendarGateway(account=account)
    profile = account.profile
    hidden_activity_ids = _hidden_activity_ids_for(trip, profile)
    body = trip_to_event_body(trip, trip_url=trip_url, hidden_activity_ids=hidden_activity_ids)

    trip_link = TripCalendarLink.objects.trip_level_link(trip, profile)
    trip_link = _upsert_event_link(gateway, account, body, trip_link, trip=trip)
    activity_count = _sync_activity_events(gateway, account, trip, trip_url=trip_url, hidden_activity_ids=hidden_activity_ids)
    return trip_link, activity_count


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
    """Push a trip's current state to every calendar it is set to auto-sync with.
    Only trip-level links with ``auto_sync`` enabled are pushed - this is one-way (UrbanLens to Google) and never pulls edits made on the Google Calendar side back in.

    Args:
        trip: The trip whose linked calendar events should be refreshed.

    Returns:
        The number of calendars the trip was successfully pushed to."""
    links = TripCalendarLink.objects.filter(trip=trip, activity__isnull=True, auto_sync=True).select_related("profile")
    synced = 0
    for link in links:
        account = GoogleCalendarAccount.objects.get_for_profile(link.profile)
        if account is None:
            continue
        try:
            export_trip_to_calendar(account, trip)
        except (GatewayRequestError, ValueError):
            logger.warning("Auto-sync of trip %s to profile %s's calendar failed.", trip.uuid, link.profile_id, exc_info=True)
            continue
        synced += 1
    return synced
