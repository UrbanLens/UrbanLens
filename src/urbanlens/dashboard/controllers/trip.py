"""Trip planning controllers."""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import SiteSettings, Trip, TripActivity, TripComment, TripMembership

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)


def _trip_or_403(trip_uuid, profile: Profile) -> Trip | HttpResponse:
    """Return the Trip if the profile is creator or member, else a 403 response."""
    trip = get_object_or_404(Trip, uuid=trip_uuid)
    if trip.creator == profile or TripMembership.objects.filter(trip=trip, profile=profile).exists():
        return trip
    return HttpResponse("Forbidden", status=403)


def _activity_qs(trip: Trip) -> QuerySet:
    """Return the standard activities queryset for a trip with all needed relations."""
    return (
        trip.activities
        .select_related("location", "pin", "pin__location", "added_by__user")
        .order_by("scheduled_at", "order", "created")
    )


def _compute_activity_index_map(activities) -> dict[int, int]:
    """Return {activity_id: map_index} for activities that have resolvable coordinates."""
    index_map: dict[int, int] = {}
    idx = 1
    for act in activities:
        has_coords = False
        if (act.pin and act.pin.effective_latitude is not None and act.pin.effective_longitude is not None) or (act.location and act.location.latitude is not None and act.location.longitude is not None):
            has_coords = True
        if has_coords:
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


def _resolve_location(body: dict):
    """Resolve a location from POST body fields.

    Priority: location_uuid → geocoded lat/lng → None.
    Creates a new Location row when geocoded coordinates are supplied.
    """
    from urbanlens.dashboard.models.location.model import Location

    location_uuid = (body.get("location_uuid") or "").strip()
    if location_uuid:
        return Location.objects.filter(uuid=location_uuid).first()

    geocoded_lat = (body.get("geocoded_lat") or "").strip()
    geocoded_lng = (body.get("geocoded_lng") or "").strip()
    if geocoded_lat and geocoded_lng:
        try:
            lat = float(geocoded_lat)
            lng = float(geocoded_lng)
            name = (body.get("geocoded_name") or body.get("title") or "Activity Location").strip() or "Activity Location"
            return Location.objects.create(name=name, latitude=lat, longitude=lng)
        except (ValueError, TypeError):
            pass

    return None


def _render_members_panel(request, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the members panel partial."""
    members = trip.memberships.select_related("profile__user").order_by("profile__user__username")
    return render(
        request,
        "dashboard/partials/trip_members_panel.html",
        {"trip": trip, "members": members, "profile": profile},
    )


def _render_activities_panel(request, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the activities panel partial with computed index map."""
    activities = list(_activity_qs(trip))
    index_map = _compute_activity_index_map(activities)
    activities_with_index = [(act, index_map.get(act.id)) for act in activities]
    return render(
        request,
        "dashboard/partials/trip_activities_panel.html",
        {"trip": trip, "activities_with_index": activities_with_index, "profile": profile},
    )


class TripListView(LoginRequiredMixin, View):
    """Trips index page and trip creation.

    GET  /trips/        → list page
    POST /trips/create/ → create a new trip, return updated list partial
    """

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        trips = (
            Trip.objects.filter(profiles=profile)
            .select_related("creator__user")
            .order_by("-created")
        )
        return render(request, "dashboard/pages/trips/index.html", {"trips": trips, "page_name": "trips"})


class TripCreateView(LoginRequiredMixin, View):
    """Create a new trip.

    POST /trips/create/  → re-renders the trip list partial
    """

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

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

        trips = (
            Trip.objects.filter(profiles=profile)
            .select_related("creator__user")
            .order_by("-created")
        )
        return render(request, "dashboard/partials/trip_list_partial.html", {"trips": trips})


class TripDetailView(LoginRequiredMixin, View):
    """Trip detail page.

    GET /trips/<uuid>/
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        return render(
            request,
            "dashboard/pages/trips/detail.html",
            {"trip": trip, "profile": profile, "page_name": "trip-detail"},
        )


class TripEditView(LoginRequiredMixin, View):
    """Edit trip metadata.

    POST /trips/<uuid>/edit/  → returns updated trip header partial
    """

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
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

        return render(request, "dashboard/partials/trip_header_partial.html", {"trip": trip, "profile": profile})


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
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_activities_panel(request, result, profile)

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        title = (body.get("title") or "").strip() or None
        notes = (body.get("notes") or "").strip() or None
        scheduled_at = _parse_scheduled_at(body.get("scheduled_date"), body.get("scheduled_time"))
        location = _resolve_location(body)

        status = (body.get("status") or "proposed").strip()
        if status not in {"proposed", "confirmed"}:
            status = "proposed"

        TripActivity.objects.create(
            trip=trip,
            location=location,
            added_by=profile,
            title=title,
            notes=notes,
            scheduled_at=scheduled_at,
            order=trip.activities.count(),
            status=status,
        )

        return _render_activities_panel(request, trip, profile)


class TripActivityEditView(LoginRequiredMixin, View):
    """Edit a trip activity.

    POST /trips/<uuid>/activities/<int:activity_id>/edit/  → re-render panel
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        activity.title = (body.get("title") or "").strip() or None
        activity.notes = (body.get("notes") or "").strip() or None
        activity.scheduled_at = _parse_scheduled_at(body.get("scheduled_date"), body.get("scheduled_time"))
        activity.location = _resolve_location(body)
        new_status = (body.get("status") or "").strip()
        if new_status in {"proposed", "confirmed"}:
            activity.status = new_status
        activity.save()

        return _render_activities_panel(request, trip, profile)


class TripActivityDeleteView(LoginRequiredMixin, View):
    """Delete a single activity and re-render the activities panel.

    DELETE /trips/<uuid>/activities/<int:activity_id>/delete/
    """

    def delete(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        activity = get_object_or_404(TripActivity, id=activity_id, trip=trip)
        activity.delete()
        return _render_activities_panel(request, trip, profile)


class TripCommentsView(LoginRequiredMixin, View):
    """Comments panel for a trip.

    GET  /trips/<uuid>/comments/  → render panel
    POST /trips/<uuid>/comments/  → add comment, re-render panel
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        comments = trip.comments.select_related("author__user").order_by("created")
        return render(request, "dashboard/partials/trip_comments_panel.html", {"trip": trip, "comments": comments, "profile": profile})

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        text = (body.get("text") or "").strip()
        if not text:
            return HttpResponse("Comment text is required.", status=400)

        TripComment.objects.create(trip=trip, author=profile, text=text)

        comments = trip.comments.select_related("author__user").order_by("created")
        return render(request, "dashboard/partials/trip_comments_panel.html", {"trip": trip, "comments": comments, "profile": profile})


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
        comment.delete()
        return HttpResponse("", status=200)


class TripMembersView(LoginRequiredMixin, View):
    """Members panel for a trip.

    GET  /trips/<uuid>/members/  → render panel
    POST /trips/<uuid>/members/  → add member by username, re-render panel
    """

    def get(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_members_panel(request, result, profile)

    def post(self, request, trip_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

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
            return HttpResponse(f'No user found with username "{username}".', status=404)

        max_members = SiteSettings.get_current().max_trip_members
        current_count = trip.profiles.count()
        if current_count >= max_members:
            return HttpResponse(
                f"This trip is full ({max_members} members maximum).", status=400,
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

        db_rows = list(
            Location.objects.filter(name__icontains=q)
            .values("uuid", "name", "locality", "administrative_area_level_1")[:5],
        )
        db_results = [
            {
                "uuid": str(row["uuid"]),
                "name": row["name"],
                "locality": row["locality"],
                "administrative_area_level_1": row["administrative_area_level_1"],
                "type": "db",
            }
            for row in db_rows
        ]

        geocoded_results: list[dict] = []
        try:
            from geopy.geocoders import Nominatim
            geolocator = Nominatim(user_agent="UrbanLens/1.0", timeout=3)
            geo_hits = geolocator.geocode(q, exactly_one=False, limit=4)
            if geo_hits:
                for hit in geo_hits:
                    geocoded_results.append({
                        "name": hit.address,
                        "lat": hit.latitude,
                        "lng": hit.longitude,
                        "type": "geocoded",
                    })
        except Exception as exc:
            logger.debug("Nominatim geocoding failed for %r: %s", q, exc)

        return JsonResponse({"results": db_results + geocoded_results})


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
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        activities = list(_activity_qs(trip))

        points = []
        index = 1
        for act in activities:
            lat = lng = None
            if act.pin:
                lat = act.pin.effective_latitude
                lng = act.pin.effective_longitude
            elif act.location:
                lat = act.location.latitude
                lng = act.location.longitude

            if lat is None or lng is None:
                continue

            label = act.title or (act.location.name if act.location else None) or f"Activity {index}"
            points.append({
                "index": index,
                "activity_id": act.id,
                "label": label,
                "lat": float(lat),
                "lng": float(lng),
                "status": act.status,
                "scheduled_at": act.scheduled_at.isoformat() if act.scheduled_at else None,
            })
            index += 1

        return JsonResponse({"points": points})


class TripActivityStatusView(LoginRequiredMixin, View):
    """Toggle or set activity status (proposed/confirmed).

    POST /trips/<uuid>/activities/<int:activity_id>/status/
    Body: {status: "proposed"|"confirmed"}
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
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

        return _render_activities_panel(request, trip, profile)


class TripActivityMoveView(LoginRequiredMixin, View):
    """Update the date of an activity (calendar drag-and-drop).

    POST /trips/<uuid>/activities/<int:activity_id>/move/
    Body: {date: "YYYY-MM-DD"}
    """

    def post(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
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
        result = _trip_or_403(trip_uuid, profile)
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
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if trip.creator == profile:
            return HttpResponse("The trip creator cannot leave — delete the trip instead.", status=400)

        TripMembership.objects.filter(trip=trip, profile=profile).delete()

        from django.urls import reverse as _reverse
        response = HttpResponse("", status=200)
        response["HX-Redirect"] = _reverse("dashboard:trips.list")
        return response
