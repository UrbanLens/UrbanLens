"""Trip planning controllers."""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import TYPE_CHECKING, Any, TypedDict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import CharField, Q
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.html import escape
from django.views import View
import requests

from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import (
    Trip,
    TripActivity,
    TripComment,
    TripMembership,
)
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits import add_visited_status, create_visit_suggestion, get_or_create_pin_at, sync_last_visited, visit_logging_allowed

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet
    from django.http import HttpRequest

    from urbanlens.dashboard.controllers.comments import _ReactionData
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

logger = logging.getLogger(__name__)


def _trips_for_list(profile: Profile) -> QuerySet[Trip]:
    """Return annotated trips for the list page.

    Args:
        profile: The viewer's profile.

    Returns:
        Queryset of trips the profile belongs to, with list stats prefetched.
    """
    return Trip.objects.for_list_page(profile)


def _apply_trip_visibility_filter(
    sensitive: list[TripActivity],
    viewer: Profile,
    hidden_out: set[int],
) -> None:
    """Populate *hidden_out* with the IDs of activities whose location the viewer
    may not see, based on each adder's trip_pin_location_visibility setting.

    Args:
        sensitive: Activities already filtered to non-ANYONE visibility and
                   non-owner viewer.
        viewer:    The profile viewing the trip.
        hidden_out: Mutable set to add hidden activity IDs into.
    """
    from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
    from urbanlens.dashboard.models.pin.model import Pin

    # Activities where the adder's account was deleted: treat as most restrictive.
    hidden_out.update(a.id for a in sensitive if a.added_by is None)
    no_one_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.NO_ONE]
    common_pin_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.COMMON_PIN]
    friends_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.FRIENDS]
    c_friend_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.COMMON_FRIEND]
    # COMMON_TRIP: viewer is already in this trip - treat as visible.

    hidden_out.update(act.id for act in no_one_acts)

    if common_pin_acts:
        loc_ids = {a.location_id for a in common_pin_acts}
        viewer_locs = set(
            Pin.objects.filter(profile=viewer, location_id__in=loc_ids).values_list("location_id", flat=True),
        )
        for act in common_pin_acts:
            if act.location_id not in viewer_locs:
                hidden_out.add(act.id)

    if friends_acts:
        adder_ids = {a.added_by_id for a in friends_acts}
        accepted_friend_adder_ids = set(
            Friendship.objects.filter(
                Q(from_profile=viewer, to_profile_id__in=adder_ids) | Q(to_profile=viewer, from_profile_id__in=adder_ids),
                status=FriendshipStatus.ACCEPTED,
            ).values_list("from_profile_id", "to_profile_id"),
        )
        # Flatten to a set of IDs that are friends with the viewer.
        flat_friend_ids: set[int] = set()
        for pair in accepted_friend_adder_ids:
            flat_friend_ids.update(pair)
        flat_friend_ids.discard(viewer.id)

        for act in friends_acts:
            if act.added_by_id not in flat_friend_ids:
                hidden_out.add(act.id)

    if c_friend_acts:
        {a.added_by_id for a in c_friend_acts}
        # Viewer's friends
        viewer_friend_ids = set(
            Friendship.objects.filter(
                Q(from_profile=viewer) | Q(to_profile=viewer),
                status=FriendshipStatus.ACCEPTED,
            ).values_list("from_profile_id", "to_profile_id"),
        )
        viewer_flat: set[int] = set()
        for pair in viewer_friend_ids:
            viewer_flat.update(pair)
        viewer_flat.discard(viewer.id)

        for act in c_friend_acts:
            # Adder's friends
            adder_friends = set(
                Friendship.objects.filter(
                    Q(from_profile_id=act.added_by_id) | Q(to_profile_id=act.added_by_id),
                    status=FriendshipStatus.ACCEPTED,
                ).values_list("from_profile_id", "to_profile_id"),
            )
            adder_flat: set[int] = set()
            for pair in adder_friends:
                adder_flat.update(pair)
            if act.added_by_id is not None:
                adder_flat.discard(act.added_by_id)

            if not (viewer_flat & adder_flat):
                hidden_out.add(act.id)


class _ReplyData(TypedDict):
    comment: TripComment
    rendered_text: str
    reactions: dict[str, _ReactionData]


class _CommentData(TypedDict):
    comment: TripComment
    rendered_text: str
    reactions: dict[str, _ReactionData]
    replies: list[_ReplyData]


def _trip_or_403(request: HttpRequest, trip_uuid: str, profile: Profile) -> Trip | HttpResponse:
    """Return the Trip if the profile is creator or member.

    Renders the same styled "not found" page for both missing trips and
    unauthorised access so users cannot enumerate private trip UUIDs.
    """
    trip = Trip.objects.filter(uuid=trip_uuid).first()
    if trip is None:
        return render(request, "dashboard/pages/trips/not_found.html", status=404)
    if trip.creator == profile or TripMembership.objects.filter(trip=trip, profile=profile).exists():
        return trip
    return render(request, "dashboard/pages/trips/not_found.html", status=403)


def _expand_trip_dates(trip: Trip, activity_date: datetime.date) -> None:
    """Expand trip date range to include activity_date if it falls outside."""
    changed = False
    if trip.start_date is None or activity_date < trip.start_date:
        trip.start_date = activity_date
        changed = True
    if trip.end_date is None or activity_date > trip.end_date:
        trip.end_date = activity_date
        changed = True
    if changed:
        trip.save(update_fields=["start_date", "end_date", "updated"])


def _activity_qs(trip: Trip) -> QuerySet:
    """Return the standard activities queryset for a trip with all needed relations."""
    from django.db.models import F

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


def _activity_coords(act: TripActivity) -> tuple[float, float] | None:
    """Return (lat, lng) for an activity, respecting override fields.

    Priority: lat_override/lng_override → pin effective coords → location coords.
    Returns None if no coordinates are available.
    """
    if act.lat_override is not None and act.lng_override is not None:
        return (act.lat_override, act.lng_override)
    if act.pin:
        lat = act.pin.effective_latitude
        lng = act.pin.effective_longitude
        if lat is not None and lng is not None:
            return (float(lat), float(lng))
    if act.location and act.location.latitude is not None and act.location.longitude is not None:
        return (float(act.location.latitude), float(act.location.longitude))
    return None


def _create_visit_entries_for_completed_activity(trip: Trip, activity: TripActivity, completer: Profile) -> None:
    """Log the completer's own visit and suggest visits to other rsvp=yes trip members.

    The completer's visit is logged immediately since completing the activity IS
    their confirmation. Every other member who RSVP'd yes gets a suggestion to
    accept or reject instead, since the system can't be sure they actually went.

    Args:
        trip: The trip the activity belongs to.
        activity: The activity that was just marked completed.
        completer: The profile who marked the activity completed.
    """
    coords = _activity_coords(activity)
    if coords is None:
        return
    lat, lng = coords

    if visit_logging_allowed(completer):
        pin = get_or_create_pin_at(completer, location=activity.location, latitude=lat, longitude=lng)
        PinVisit.objects.create(pin=pin, visited_at=activity.scheduled_at, source=VisitSource.TRIP)
        sync_last_visited(pin)
        add_visited_status(pin)

    if activity.scheduled_at is not None:
        other_yes = list(TripMembership.objects.filter(trip=trip, rsvp=TripMembership.RSVP_YES).exclude(profile=completer).select_related("profile"))
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


def _compute_activity_index_map(activities: Iterable[TripActivity]) -> dict[int, int]:
    """Return {activity_id: map_index} for activities visible on the map (excludes completed/hidden)."""
    index_map: dict[int, int] = {}
    idx = 1
    for act in activities:
        if _activity_coords(act) is not None and not act.location_hidden and act.status != TripActivity.STATUS_COMPLETED:
            index_map[act.id] = idx
            idx += 1
    return index_map


def _parse_scheduled_at(date_str: str | None, time_str: str | None) -> datetime.datetime | None:
    """Combine separate date and time strings into a datetime.

    If only a date is provided, midnight (00:00) is used as the time so the
    caller can distinguish "date only" from "date + time" by inspecting the
    time component.  Returns None when no date is given.
    """
    if not date_str:
        return None
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None
    if time_str:
        try:
            t = datetime.time.fromisoformat(time_str)
        except ValueError:
            t = datetime.time(0, 0)
    else:
        t = datetime.time(0, 0)
    return datetime.datetime.combine(d, t)


def _resolve_activity_place(body: dict[str, Any], profile: Profile) -> tuple[Location | None, Any | None]:
    """Resolve an activity target from submitted location fields.

    Priority: selected pin → selected shared location → supplied coordinates/address.
    Geocoded or raw coordinate entries create a Location row for the activity.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

    pin_uuid = (body.get("pin_uuid") or "").strip()
    if pin_uuid:
        pin = Pin.objects.filter(profile=profile, uuid=pin_uuid).select_related("location").first()
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
            from urbanlens.dashboard.models.wiki.model import Wiki

            location, _ = Location.objects.get_or_create(
                latitude=lat,
                longitude=lng,
                defaults={"official_name": name or "Activity Location"},
            )
            # Trips are not-private by design, so ensure we have a wiki page for it. (TODO: What about single-person trips?)
            Wiki.objects.get_or_create_for_location(location, defaults={"name": name or "Activity Location"})
            return location, None
        except (ValueError, TypeError):
            pass

    return None, None


def _is_organizer(profile: Profile, trip: Trip) -> bool:
    """Return True if profile is the trip creator or a designated organizer."""
    if trip.creator_id == profile.id:
        return True
    return TripMembership.objects.filter(trip=trip, profile=profile, is_organizer=True).exists()


def _can_perform(profile: Profile, trip: Trip, level: str) -> bool:
    """Return True if profile is allowed to act at the given permission level.

    Organizers and the creator are always allowed. 'everyone' allows any member.
    'organizers' requires organizer/creator status. 'none' allows only the creator.
    """
    if trip.creator_id == profile.id:
        return True
    if level == Trip.PERM_EVERYONE:
        return True
    if level == Trip.PERM_ORGANIZERS:
        return TripMembership.objects.filter(trip=trip, profile=profile, is_organizer=True).exists()
    return False


def _render_members_panel(request: HttpRequest, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the members panel partial."""
    members = trip.memberships.select_related("profile__user").order_by("profile__user__username")
    return render(
        request,
        "dashboard/partials/trips/trip_members_panel.html",
        {"trip": trip, "members": members, "profile": profile},
    )


def _render_activities_panel(request: HttpRequest, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the activities panel with index map, vote counts, and per-activity permissions."""
    from urbanlens.dashboard.models.trips.model import TripActivity, TripActivityVote

    activities = list(_activity_qs(trip))
    index_map = _compute_activity_index_map(activities)

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
        if v["profile_id"] == profile.id:
            user_votes[aid] = v["vote"]

    # Determine which activities have their location hidden from this viewer
    # based on the adder's trip_pin_location_visibility privacy setting.
    viewer_hidden: set[int] = set()
    sensitive = [act for act in activities if not act.location_hidden and act.added_by_id and act.added_by_id != profile.id and act.added_by and act.added_by.trip_pin_location_visibility != VisibilityChoice.ANYONE and act.location_id]
    if sensitive:
        _apply_trip_visibility_filter(sensitive, profile, viewer_hidden)

    viewer_is_organizer = _is_organizer(profile, trip)
    activities_with_index = [
        {
            "activity": act,
            "index": index_map.get(act.id),
            "vote_up": up_counts.get(act.id, 0),
            "vote_down": down_counts.get(act.id, 0),
            "user_vote": user_votes.get(act.id),
            "can_manage": (act.added_by_id == profile.id or viewer_is_organizer),
            "effective_location_hidden": act.location_hidden or (act.id in viewer_hidden),
        }
        for act in activities
    ]
    all_activities_completed = bool(activities) and all(act.status == TripActivity.STATUS_COMPLETED for act in activities)
    return render(
        request,
        "dashboard/partials/trips/trip_activities_panel.html",
        {
            "trip": trip,
            "activities_with_index": activities_with_index,
            "profile": profile,
            "all_activities_completed": all_activities_completed,
        },
    )


class TripListView(LoginRequiredMixin, View):
    """Trips index page and trip creation.

    GET  /trips/        → list page
    POST /trips/create/ → create a new trip, return updated list partial
    """

    def get(self, request):
        from urbanlens.dashboard.services.connections import get_connections

        profile, _ = Profile.objects.get_or_create(user=request.user)
        trips = _trips_for_list(profile)
        friends = get_connections(profile)
        return render(
            request,
            "dashboard/pages/trips/index.html",
            {"trips": trips, "profile": profile, "page_name": "trips", "friends": friends},
        )


class TripCreateView(LoginRequiredMixin, View):
    """Create a new trip.

    POST /trips/create/  → re-renders the trip list partial
    """

    def post(self, request):
        from urbanlens.dashboard.services.connections import get_connections

        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body) if request.body else {}
            invite_ids = body.get("invite_profile_ids") or []
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()
            invite_ids = request.POST.getlist("invite_profile_ids")

        name = (body.get("name") or "").strip()
        if not name:
            return HttpResponse("Trip name is required.", status=400)

        trip = Trip.objects.create(
            name=name,
            description=body.get("description") or None,
            start_date=body.get("start_date") or None,
            end_date=body.get("end_date") or None,
            creator=profile,
        )
        TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"rsvp": "yes"})

        # Only invite accepted friends - never trust arbitrary submitted profile IDs.
        if invite_ids:
            friend_ids = {str(f.id) for f in get_connections(profile)}
            selected_ids = {pid for pid in invite_ids if str(pid) in friend_ids}
            if selected_ids:
                max_members = SiteSettings.get_current().max_trip_members
                remaining = max_members - trip.profiles.count()
                for friend_profile in Profile.objects.filter(id__in=selected_ids)[:remaining]:
                    TripMembership.objects.get_or_create(trip=trip, profile=friend_profile)

        trips = _trips_for_list(profile)
        return render(request, "dashboard/partials/trips/trip_list_partial.html", {"trips": trips, "profile": profile})


class TripDetailView(LoginRequiredMixin, View):
    """Trip detail page.

    GET /trips/<uuid>/
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        return render(
            request,
            "dashboard/pages/trips/detail.html",
            {
                "trip": trip,
                "profile": profile,
                "page_name": "trip-detail",
                "viewer_is_organizer": _is_organizer(profile, trip),
                **profile.get_map_center_template_context(),
            },
        )


class TripEditView(LoginRequiredMixin, View):
    """Edit trip metadata.

    POST /trips/<uuid>/edit/  → returns updated trip header partial
    """

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        name = (body.get("name") or "").strip()
        if name:
            trip.name = name
        trip.description = body.get("description") or None
        trip.start_date = body.get("start_date") or None
        trip.end_date = body.get("end_date") or None
        trip.save()

        return render(
            request,
            "dashboard/partials/trips/trip_header_partial.html",
            {
                "trip": trip,
                "profile": profile,
                "viewer_is_organizer": _is_organizer(profile, trip),
            },
        )


class TripDeleteView(LoginRequiredMixin, View):
    """Delete a trip (creator only).

    DELETE /trips/<uuid>/delete/
    """

    def delete(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        trip = get_object_or_404(Trip, uuid=trip_uuid)
        if trip.creator != profile:
            return HttpResponse("Only the trip creator can delete it.", status=403)
        trip.delete()
        return HttpResponse("", status=200)


class TripActivitiesView(LoginRequiredMixin, View):
    """Activities panel for a trip.

    GET  /trips/<uuid>/activities/  → render panel
    POST /trips/<uuid>/activities/  → add activity, re-render panel
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_activities_panel(request, result, profile)

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _can_perform(profile, trip, trip.allow_add_activities):
            return HttpResponse("You don't have permission to add activities to this trip.", status=403)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        title = (body.get("title") or "").strip() or None
        notes = (body.get("notes") or "").strip() or None
        scheduled_at = _parse_scheduled_at(body.get("scheduled_date"), body.get("scheduled_time"))
        scheduled_end = _parse_scheduled_at(body.get("scheduled_end_date"), body.get("scheduled_end_time"))
        location, pin = _resolve_activity_place(body, profile)

        child_trip_uuid = (body.get("child_trip_uuid") or "").strip()
        child_trip = Trip.objects.filter(uuid=child_trip_uuid).first() if child_trip_uuid else None

        status = (body.get("status") or "proposed").strip()
        if status not in {"proposed", "confirmed"}:
            status = "proposed"

        location_hidden = body.get("location_hidden") in {"true", "1", "on", True}

        TripActivity.objects.create(
            trip=trip,
            location=location,
            pin=pin,
            added_by=profile,
            title=title,
            notes=notes,
            scheduled_at=scheduled_at,
            scheduled_end=scheduled_end,
            order=trip.activities.count(),
            status=status,
            child_trip=child_trip,
            location_hidden=location_hidden,
        )

        return _render_activities_panel(request, trip, profile)


class TripActivityEditView(LoginRequiredMixin, View):
    """Edit a trip activity.

    POST /trips/<uuid>/activities/<int:activity_id>/edit/  → re-render panel
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _can_perform(profile, trip, trip.allow_edit_activities):
            return HttpResponse("You don't have permission to edit activities on this trip.", status=403)

        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        activity.title = (body.get("title") or "").strip() or None
        activity.notes = (body.get("notes") or "").strip() or None
        activity.scheduled_at = _parse_scheduled_at(body.get("scheduled_date"), body.get("scheduled_time"))
        activity.scheduled_end = _parse_scheduled_at(body.get("scheduled_end_date"), body.get("scheduled_end_time"))
        activity.location, activity.pin = _resolve_activity_place(body, profile)
        new_status = (body.get("status") or "").strip()
        if new_status in {"proposed", "confirmed"}:
            activity.status = new_status

        child_trip_uuid = (body.get("child_trip_uuid") or "").strip()
        if child_trip_uuid:
            activity.child_trip = Trip.objects.filter(uuid=child_trip_uuid).first()
        elif "child_trip_uuid" in body:
            activity.child_trip = None

        # location_hidden may only be changed by the activity creator or trip organizers
        if activity.added_by_id == profile.id or _is_organizer(profile, trip):
            activity.location_hidden = body.get("location_hidden") in {"true", "1", "on", True}

        activity.save()

        if activity.status == "confirmed" and activity.scheduled_at:
            _expand_trip_dates(trip, activity.scheduled_at.date())

        return _render_activities_panel(request, trip, profile)


class TripActivityDeleteView(LoginRequiredMixin, View):
    """Delete a single activity and re-render the activities panel.

    DELETE /trips/<uuid>/activities/<int:activity_id>/delete/
    """

    def delete(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _can_perform(profile, trip, trip.allow_edit_activities):
            return HttpResponse("You don't have permission to delete activities on this trip.", status=403)

        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)
        activity.delete()
        return _render_activities_panel(request, trip, profile)


class TripActivityCompleteView(LoginRequiredMixin, View):
    """Mark an activity as completed, snapping its date to today if it was in the future.

    POST /trips/<uuid>/activities/<int:activity_id>/complete/
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)
        already_completed = activity.status == TripActivity.STATUS_COMPLETED

        today = datetime.date.today()
        completed_date_str = request.POST.get("completed_date", "")
        if completed_date_str:
            try:
                completed_date = datetime.date.fromisoformat(completed_date_str)
                completed_date = min(completed_date, today)
            except ValueError:
                completed_date = today
        else:
            completed_date = today

        activity.scheduled_at = datetime.datetime.combine(
            completed_date,
            activity.scheduled_at.time() if activity.scheduled_at else datetime.time(0, 0),
        )
        activity.status = TripActivity.STATUS_COMPLETED
        activity.save(update_fields=["status", "scheduled_at", "updated"])
        if not already_completed:
            _create_visit_entries_for_completed_activity(trip, activity, profile)

        return _render_activities_panel(request, trip, profile)


class TripActivityVoteView(LoginRequiredMixin, View):
    """Cast, update, or clear a member's vote on a proposed activity.

    POST /trips/<uuid>/activities/<int:activity_id>/vote/
    Form body: vote=up | vote=down | vote= (empty to clear)
    """

    def post(self, request, trip_uuid, activity_id):
        """Handle a vote submission and re-render the activities panel.

        Args:
            request: The HTTP request.
            trip_uuid: The trip UUID.
            activity_id: The activity ID.

        Returns:
            Re-rendered activities panel or an error response.
        """
        from urbanlens.dashboard.models.trips.model import TripActivityVote

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)

        if activity.status != TripActivity.STATUS_PROPOSED:
            return HttpResponse("Voting is only available for proposed activities.", status=400)

        vote_value = request.POST.get("vote", "").strip()

        if not vote_value:
            TripActivityVote.objects.filter(activity=activity, profile=profile).delete()
        elif vote_value in {TripActivityVote.VOTE_UP, TripActivityVote.VOTE_DOWN}:
            TripActivityVote.objects.update_or_create(
                activity=activity,
                profile=profile,
                defaults={"vote": vote_value},
            )
        else:
            return HttpResponse("Invalid vote value.", status=400)

        return _render_activities_panel(request, trip, profile)


def _render_trip_comments(request: HttpRequest, trip: Trip, profile: Profile) -> HttpResponse:
    """Build comment panel context with activity mentions and re-render."""
    from urbanlens.dashboard.controllers.comments import (
        _ALLOWED_EMOJIS,
        _aggregate_reactions,
    )
    from urbanlens.dashboard.services.mentions import (
        render_comment_text,
        viewer_pinned_uuids,
    )

    activities = list(_activity_qs(trip))
    index_map = _compute_activity_index_map(activities)
    act_by_index = {v: a for a, v in index_map.items()}
    act_objects = {a.id: a for a in activities}
    act_index_for_render = {idx: act_objects[act_id] for idx, act_id in act_by_index.items()}

    pinned = viewer_pinned_uuids(profile)
    top_comments = (
        trip.comments.filter(parent__isnull=True)
        .select_related("author__user", "markup_map")
        # comment.map_data derives its snapshot from the markup map's items.
        .prefetch_related("reactions", "replies__reactions", "replies__author__user", "markup_map__items", "replies__markup_map__items")
        .order_by("created")
    )

    rendered: list[_CommentData] = []
    for c in top_comments:
        html = render_comment_text(c.text, pinned, act_index_for_render)
        if html is None:
            continue
        reactions = _aggregate_reactions(c.reactions.all())
        replies_rendered: list[_ReplyData] = []
        for r in c.replies.all():
            r_html = render_comment_text(r.text, pinned, act_index_for_render)
            if r_html is None:
                continue
            replies_rendered.append(
                {
                    "comment": r,
                    "rendered_text": r_html,
                    "reactions": _aggregate_reactions(r.reactions.all()),
                },
            )
        rendered.append(
            {
                "comment": c,
                "rendered_text": html,
                "reactions": reactions,
                "replies": replies_rendered,
            },
        )

    comment_count = sum(1 + len(item["replies"]) for item in rendered)
    return render(
        request,
        "dashboard/partials/trips/trip_comments_panel.html",
        {
            "trip": trip,
            "rendered_comments": rendered,
            "comment_count": comment_count,
            "profile": profile,
            "allowed_emojis": _ALLOWED_EMOJIS,
        },
    )


class TripCommentsView(LoginRequiredMixin, View):
    """Comments panel for a trip.

    GET  /trips/<uuid>/comments/  → render panel
    POST /trips/<uuid>/comments/  → add comment, re-render panel
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_trip_comments(request, result, profile)

    def post(self, request, trip_uuid):
        from urbanlens.dashboard.controllers.comments import _notify_reply

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _can_perform(profile, trip, trip.allow_comments):
            return HttpResponse("You don't have permission to comment on this trip.", status=403)

        text = request.POST.get("text", "").strip()
        image = request.FILES.get("image")
        from urbanlens.dashboard.controllers.comments import _parse_map_data
        from urbanlens.dashboard.services.map_snapshot import materialize_markup_map

        map_data = _parse_map_data(request)
        if not text and not image and not map_data:
            return HttpResponse("Please add some text, a photo, or a map.", status=400)

        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = get_object_or_404(TripComment, id=parent_id, trip=trip)

        comment = TripComment.objects.create(trip=trip, author=profile, text=text, parent=parent, markup_map=materialize_markup_map(profile, map_data))
        if image:
            comment.image = image
            comment.save(update_fields=["image"])

        if parent and parent.author and parent.author != profile:
            _notify_reply(profile, parent, reply=comment)

        return _render_trip_comments(request, trip, profile)


class TripCommentDeleteView(LoginRequiredMixin, View):
    """Delete a comment (author or trip creator only).

    DELETE /trips/<uuid>/comments/<int:comment_id>/delete/
    """

    def delete(self, request, trip_uuid, comment_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        trip = get_object_or_404(Trip, uuid=trip_uuid)
        if not (trip.creator == profile or trip.profiles.filter(pk=profile.pk).exists()):
            return HttpResponse("Forbidden", status=403)
        comment = get_object_or_404(TripComment, id=comment_id, trip=trip)
        if profile not in {comment.author, trip.creator}:
            return HttpResponse("You can only delete your own comments.", status=403)
        markup_map = comment.markup_map
        comment.delete()
        if markup_map is not None:
            markup_map.delete()
        return _render_trip_comments(request, trip, profile)


class TripMembersView(LoginRequiredMixin, View):
    """Members panel for a trip.

    GET  /trips/<uuid>/members/  → render panel
    POST /trips/<uuid>/members/  → add member by username, re-render panel
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_members_panel(request, result, profile)

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _can_perform(profile, trip, trip.allow_add_members):
            return HttpResponse("You don't have permission to add members to this trip.", status=403)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        username = (body.get("username") or "").strip()
        if not username:
            return HttpResponse("Username is required.", status=400)

        from django.contrib.auth.models import User

        try:
            user = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            return HttpResponse(f'No user found with username "{escape(username)}".', status=404)

        max_members = SiteSettings.get_current().max_trip_members
        current_count = trip.profiles.count()
        if current_count >= max_members:
            return HttpResponse(
                f"This trip is full ({max_members} members maximum).",
                status=400,
            )

        new_profile, _ = Profile.objects.get_or_create(user=user)
        TripMembership.objects.get_or_create(trip=trip, profile=new_profile)

        return _render_members_panel(request, trip, profile)


class TripMemberRemoveView(LoginRequiredMixin, View):
    """Remove a member from a trip.

    DELETE /trips/<uuid>/members/<int:profile_id>/remove/
    Only the trip creator may remove members (members can remove themselves).
    """

    def delete(self, request, trip_uuid, profile_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        trip = get_object_or_404(Trip, uuid=trip_uuid)
        if not (trip.creator == profile or trip.profiles.filter(pk=profile.pk).exists()):
            return HttpResponse("Forbidden", status=403)
        target = get_object_or_404(Profile, pk=profile_id)
        if target == trip.creator:
            return HttpResponse("The trip creator cannot be removed.", status=400)
        if profile not in {target, trip.creator}:
            return HttpResponse("Only the trip creator can remove other members.", status=403)
        TripMembership.objects.filter(trip=trip, profile=target).delete()

        return _render_members_panel(request, trip, profile)


class TripMemberOrganizerView(LoginRequiredMixin, View):
    """Toggle organizer status for a trip member (creator only).

    POST /trips/<uuid>/members/<int:profile_id>/organizer/
    """

    def post(self, request, trip_uuid, profile_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if trip.creator_id != profile.id:
            return HttpResponse("Only the trip creator can manage organizers.", status=403)

        target = get_object_or_404(Profile, pk=profile_id)
        if target.id == trip.creator_id:
            return HttpResponse("The trip creator is always an organizer.", status=400)

        membership = get_object_or_404(TripMembership, trip=trip, profile=target)
        membership.is_organizer = not membership.is_organizer
        membership.save(update_fields=["is_organizer", "updated"])

        return _render_members_panel(request, trip, profile)


class TripLocationSearchView(LoginRequiredMixin, View):
    """JSON search for locations to add as activities.

    Searches existing Location records by name, then falls back to Nominatim
    geocoding so the user can enter arbitrary addresses.

    GET /trips/location-search/?q=<query>
    """

    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})

        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        # The separator is a single merged character class rather than
        # `\s*[, ]\s*` - adjacent `\s*` groups flanking a class that also
        # matches whitespace let a run of spaces be split among the three
        # groups in many equivalent ways, which is a polynomial ReDoS.
        coordinate_match = re.match(r"^\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)\s*$", q) if len(q) <= 64 else None
        coordinate_results: list[dict] = []
        if coordinate_match:
            lat = float(coordinate_match.group(1))
            lng = float(coordinate_match.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                coordinate_results.append({"name": f"{lat:.6f}, {lng:.6f}", "lat": lat, "lng": lng, "type": "coordinates"})

        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin_rows = list(
            Pin.objects.filter(profile=profile).filter(Q(name__icontains=q) | Q(location__official_name__icontains=q) | Q(location__wiki__name__icontains=q) | Q(description__icontains=q)).select_related("location")[:5],
        )
        pin_results = [
            {
                "uuid": str(pin.uuid),
                "name": pin.effective_name or "Untitled pin",
                "locality": pin.locality,
                "administrative_area_level_1": pin.administrative_area_level_1,
                "type": "pin",
            }
            for pin in pin_rows
        ]

        db_rows = list(
            Location.objects.filter(Q(wiki__name__icontains=q) | Q(official_name__icontains=q))
            .annotate(display_name=Coalesce("wiki__name", "official_name", output_field=CharField()))
            .values(
                "uuid",
                "display_name",
                "locality",
                "administrative_area_level_1",
            )[:5],
        )
        db_results = [
            {
                "uuid": str(row["uuid"]),
                "name": row["display_name"] or "Unnamed location",
                "display_name": row["display_name"] or "Unnamed location",
                "locality": row["locality"],
                "administrative_area_level_1": row["administrative_area_level_1"],
                "type": "db",
            }
            for row in db_rows
        ]

        geocoded_results: list[dict] = []
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if profile.external_apis_enabled:
            try:
                from geopy.geocoders import Nominatim

                geolocator = Nominatim(user_agent="UrbanLens/1.0", timeout=3)
                geo_hits = geolocator.geocode(q, exactly_one=False, limit=4)
                if geo_hits:
                    for hit in geo_hits:
                        geocoded_results.append(
                            {
                                "name": hit.address,
                                "lat": hit.latitude,
                                "lng": hit.longitude,
                                "type": "geocoded",
                            },
                        )
            except (ImportError, OSError, ValueError) as exc:
                logger.debug("Nominatim geocoding failed for %r: %s", q, exc)

        return JsonResponse({"results": coordinate_results + pin_results + db_results + geocoded_results})


class TripMapDataView(LoginRequiredMixin, View):
    """Return GeoJSON-style activity data for the trip map.

    GET /trips/<uuid>/map-data/
    """

    def get(self, request, trip_uuid):
        """Return activity locations with coordinates as JSON.

        Args:
            request: The HTTP request.
            trip_uuid: The trip UUID.

        Returns:
            JsonResponse with a list of activity points.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        activities = list(_activity_qs(trip))

        # Determine activities viewer-hidden due to adder's privacy setting
        viewer_hidden_map: set[int] = set()
        sensitive_map = [act for act in activities if not act.location_hidden and act.added_by_id and act.added_by_id != profile.id and act.added_by and act.added_by.trip_pin_location_visibility != VisibilityChoice.ANYONE and act.location_id]
        if sensitive_map:
            _apply_trip_visibility_filter(sensitive_map, profile, viewer_hidden_map)

        include_past = request.GET.get("include_past", "0") not in {"", "0", "false"}

        points = []
        index = 1
        seen_child_acts: set[int] = set()

        for act in activities:
            if act.status == TripActivity.STATUS_COMPLETED and not include_past:
                continue

            coords = _activity_coords(act)

            if coords and not act.location_hidden and act.id not in viewer_hidden_map:
                label = act.effective_title
                points.append(
                    {
                        "index": index,
                        "activity_id": act.id,
                        "label": label,
                        "lat": coords[0],
                        "lng": coords[1],
                        "status": act.status,
                        "scheduled_at": act.scheduled_at.isoformat() if act.scheduled_at else None,
                        "draggable": True,
                    },
                )
                index += 1

            # Include child trip's activities as ghost markers
            if act.child_trip_id and act.child_trip_id not in seen_child_acts:
                seen_child_acts.add(act.child_trip_id)
                child_acts = list(_activity_qs(act.child_trip))
                for child_act in child_acts:
                    child_coords = _activity_coords(child_act)
                    if not child_coords:
                        continue
                    child_label = child_act.effective_title
                    points.append(
                        {
                            "index": None,
                            "activity_id": None,
                            "label": f"[{act.child_trip.name}] {child_label}",
                            "lat": child_coords[0],
                            "lng": child_coords[1],
                            "status": child_act.status,
                            "scheduled_at": child_act.scheduled_at.isoformat() if child_act.scheduled_at else None,
                            "draggable": False,
                            "child_trip": True,
                        },
                    )

        return JsonResponse({"points": points})


class TripActivityStatusView(LoginRequiredMixin, View):
    """Toggle or set activity status (proposed/confirmed).

    POST /trips/<uuid>/activities/<int:activity_id>/status/
    Body: {status: "proposed"|"confirmed"}
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        new_status = (body.get("status") or "").strip()
        if new_status not in {"proposed", "confirmed"}:
            # Toggle
            new_status = "confirmed" if activity.status == "proposed" else "proposed"

        activity.status = new_status
        activity.save(update_fields=["status", "updated"])

        if new_status == "confirmed" and activity.scheduled_at:
            _expand_trip_dates(trip, activity.scheduled_at.date())

        return _render_activities_panel(request, trip, profile)


class TripActivityMoveView(LoginRequiredMixin, View):
    """Update the date of an activity (calendar drag-and-drop).

    POST /trips/<uuid>/activities/<int:activity_id>/move/
    Body: {date: "YYYY-MM-DD"}
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        date_str = (body.get("date") or "").strip()
        if not date_str:
            return HttpResponse("date is required.", status=400)

        try:
            new_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return HttpResponse("Invalid date format.", status=400)

        if activity.scheduled_at:
            # Preserve existing time component; only update date
            activity.scheduled_at = datetime.datetime.combine(new_date, activity.scheduled_at.time())
        else:
            activity.scheduled_at = datetime.datetime.combine(new_date, datetime.time(0, 0))

        activity.save(update_fields=["scheduled_at", "updated"])

        return _render_activities_panel(request, trip, profile)


class TripMemberRSVPView(LoginRequiredMixin, View):
    """Set RSVP status for the current user on a trip.

    POST /trips/<uuid>/rsvp/
    Body: {rsvp: "yes"|"no"|"maybe"|""}
    """

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        rsvp = (body.get("rsvp") or "").strip()
        if rsvp not in {"yes", "no", "maybe", ""}:
            return HttpResponse("Invalid RSVP value.", status=400)

        membership = get_object_or_404(TripMembership, trip=trip, profile=profile)
        membership.rsvp = rsvp or None
        membership.save(update_fields=["rsvp", "updated"])

        return _render_members_panel(request, trip, profile)


class TripLeaveView(LoginRequiredMixin, View):
    """Leave a trip (non-creator members only).

    DELETE /trips/<uuid>/leave/
    """

    def delete(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if trip.creator == profile:
            return HttpResponse("The trip creator cannot leave - delete the trip instead.", status=400)

        TripMembership.objects.filter(trip=trip, profile=profile).delete()

        from django.urls import reverse as _reverse

        response = HttpResponse("", status=200)
        response["HX-Redirect"] = _reverse("trips.list")
        return response


class TripSettingsView(LoginRequiredMixin, View):
    """Save trip settings (creator only).

    POST /trips/<uuid>/settings/
    """

    def post(self, request, trip_uuid):
        """Handle POST to update trip permission settings.

        Args:
            request: The HTTP request.
            trip_uuid: The trip UUID.

        Returns:
            Rendered settings partial on success, or an error HttpResponse.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _is_organizer(profile, trip):
            return HttpResponse("Only the trip creator or an organizer can change settings.", status=403)

        valid_levels = {Trip.PERM_NONE, Trip.PERM_ORGANIZERS, Trip.PERM_EVERYONE}

        def _level(key: str, default: str) -> str:
            val = (request.POST.get(key) or "").strip()
            return val if val in valid_levels else default

        trip.allow_add_members = _level("allow_add_members", Trip.PERM_NONE)
        trip.allow_add_activities = _level("allow_add_activities", Trip.PERM_EVERYONE)
        trip.allow_edit_activities = _level("allow_edit_activities", Trip.PERM_EVERYONE)
        trip.allow_comments = _level("allow_comments", Trip.PERM_EVERYONE)
        trip.save(
            update_fields=[
                "allow_add_members",
                "allow_add_activities",
                "allow_edit_activities",
                "allow_comments",
                "updated",
            ],
        )

        return render(
            request,
            "dashboard/partials/trips/trip_settings_partial.html",
            {
                "trip": trip,
                "profile": profile,
                "saved": True,
            },
        )


class TripActivityPositionView(LoginRequiredMixin, View):
    """Save a map-drag position override for a trip activity.

    POST /trips/<uuid>/activities/<int:activity_id>/position/
    Body: {lat: float, lng: float}
    This updates lat_override/lng_override on the TripActivity only - the
    underlying Pin and Location coordinates are never modified.
    """

    def post(self, request, trip_uuid, activity_id):
        """Handle POST to update map position override.

        Args:
            request: The HTTP request.
            trip_uuid: The trip UUID.
            activity_id: The TripActivity primary key.

        Returns:
            JsonResponse confirming saved coordinates, or an error HttpResponse.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            lat = float(body["lat"])
            lng = float(body["lng"])
        except (KeyError, TypeError, ValueError):
            return HttpResponse("lat and lng are required.", status=400)

        activity.lat_override = lat
        activity.lng_override = lng
        activity.save(update_fields=["lat_override", "lng_override", "updated"])

        return JsonResponse({"lat": lat, "lng": lng})


class TripChildTripSearchView(LoginRequiredMixin, View):
    """Search for trips the current user can add as a child activity.

    Only trips the user is a member of (excluding the current trip) are returned.

    GET /trips/<uuid>/child-trip-search/?q=<query>
    """

    def get(self, request, trip_uuid):
        """Return JSON list of matching trips.

        Args:
            request: The HTTP request.
            trip_uuid: The parent trip UUID (to exclude it from results).

        Returns:
            JsonResponse with a list of matching trip objects.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})

        trips = Trip.objects.filter(profiles=profile, name__icontains=q).exclude(uuid=trip_uuid).order_by("name")[:8]
        results = [
            {
                "uuid": str(t.uuid),
                "name": t.name,
                "start_date": t.effective_start_date.isoformat() if t.effective_start_date else None,
                "end_date": t.effective_end_date.isoformat() if t.effective_end_date else None,
            }
            for t in trips
        ]
        return JsonResponse({"results": results})


def _build_activity_forecasts(activities: list[TripActivity], gateway: OpenWeatherMapGateway) -> list[dict]:
    """For each activity, find the closest 3-hourly forecast slot at its location/time.

    Returns a list of dicts with keys:
      activity, location_name, scheduled_at, slot, no_coords, out_of_range
    """
    cache: dict[tuple[float, float], list[dict] | None] = {}
    results = []

    for act in activities:
        coords = _activity_coords(act)

        location_name = act.effective_title if act.effective_title != "Unnamed activity" else ""

        entry: dict = {
            "activity": act,
            "location_name": location_name,
            "scheduled_at": act.scheduled_at,
            "slot": None,
            "no_coords": coords is None,
            "out_of_range": False,
        }

        if coords is None or act.scheduled_at is None:
            results.append(entry)
            continue

        key = (round(coords[0], 2), round(coords[1], 2))
        if key not in cache:
            try:
                cache[key] = gateway.get_raw_forecast(*coords)
            except requests.RequestException:
                logger.warning("Weather fetch failed for coords %s", key)
                cache[key] = None

        slots = cache.get(key) or []
        if not slots:
            results.append(entry)
            continue

        target = act.scheduled_at
        if target.tzinfo is not None:
            target = target.replace(tzinfo=None)

        closest = min(slots, key=lambda s: abs((s["date"] - target).total_seconds()))
        gap_hours = abs((closest["date"] - target).total_seconds()) / 3600

        if gap_hours > 36:
            entry["out_of_range"] = True
        else:
            entry["slot"] = closest

        results.append(entry)

    return results


class TripWeatherView(LoginRequiredMixin, View):
    """Render the weather forecast panel for a trip.

    GET /trips/<uuid>/weather/
    """

    def get(self, request, trip_uuid):
        """Return weather HTML partial for the trip.

        Args:
            request: The HTTP request.
            trip_uuid: The trip UUID.

        Returns:
            Rendered weather partial or an error response.
        """
        from collections import defaultdict

        from urbanlens.dashboard.services.apis.weather.gateway import (
            OpenWeatherMapGateway,
        )
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(request, trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        error: str = ""
        grouped: list[tuple] = []

        if not profile.external_apis_enabled:
            error = "External weather lookups are turned off in your settings."
        elif not app_settings.openweathermap_api_key:
            error = "Weather API key not configured."
        else:
            today = datetime.date.today()
            activities = [act for act in _activity_qs(trip) if act.status != TripActivity.STATUS_COMPLETED and (act.scheduled_at is None or act.scheduled_at.date() >= today)]
            if not activities:
                pass  # no upcoming activities - leave error/grouped empty to hide the section
            else:
                try:
                    gateway = OpenWeatherMapGateway()
                    activity_forecasts = _build_activity_forecasts(activities, gateway)

                    day_map: dict = defaultdict(list)
                    for af in activity_forecasts:
                        day = af["scheduled_at"].date() if af["scheduled_at"] else None
                        day_map[day].append(af)

                    dated = sorted(d for d in day_map if d is not None)
                    keys = dated + ([None] if None in day_map else [])
                    grouped = [(d, day_map[d]) for d in keys]
                except (requests.RequestException, KeyError, TypeError):
                    logger.warning("Weather fetch failed for trip %s", trip_uuid)
                    error = "Weather data could not be loaded."

        return render(
            request,
            "dashboard/pages/trips/trip_weather.html",
            {
                "trip": trip,
                "grouped": grouped,
                "error": error,
            },
        )
