"""Trip planning controllers."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment

logger = logging.getLogger(__name__)


def _trip_or_403(trip_uuid, profile: Profile) -> Trip | HttpResponse:
    """Return the Trip if the profile is creator or member, else a 403 response."""
    trip = get_object_or_404(Trip, uuid=trip_uuid)
    if trip.creator == profile or trip.profiles.filter(pk=profile.pk).exists():
        return trip
    return HttpResponse("Forbidden", status=403)


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
        trip.profiles.add(profile)

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
        trip = result
        activities = trip.activities.select_related("location", "pin", "added_by__user").order_by("scheduled_at", "order", "created")
        return render(request, "dashboard/partials/trip_activities_panel.html", {"trip": trip, "activities": activities, "profile": profile})

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
        scheduled_at = body.get("scheduled_at") or None

        location = None
        location_uuid = body.get("location_uuid")
        if location_uuid:
            from urbanlens.dashboard.models.location.model import Location
            location = Location.objects.filter(uuid=location_uuid).first()

        TripActivity.objects.create(
            trip=trip,
            location=location,
            added_by=profile,
            title=title,
            notes=notes,
            scheduled_at=scheduled_at,
            order=trip.activities.count(),
        )

        activities = trip.activities.select_related("location", "pin", "added_by__user").order_by("scheduled_at", "order", "created")
        return render(request, "dashboard/partials/trip_activities_panel.html", {"trip": trip, "activities": activities, "profile": profile})


class TripActivityDeleteView(LoginRequiredMixin, View):
    """Delete a single activity.

    DELETE /trips/<uuid>/activities/<int:activity_id>/delete/
    """

    def delete(self, request, trip_uuid, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = _trip_or_403(trip_uuid, profile)
        if isinstance(result, HttpResponse):
            return result
        activity = get_object_or_404(TripActivity, id=activity_id, trip=result)
        activity.delete()
        return HttpResponse("", status=200)


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
        trip = result
        members = trip.profiles.select_related("user").order_by("user__username")
        return render(request, "dashboard/partials/trip_members_panel.html", {"trip": trip, "members": members, "profile": profile})

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

        new_profile, _ = Profile.objects.get_or_create(user=user)
        trip.profiles.add(new_profile)

        members = trip.profiles.select_related("user").order_by("user__username")
        return render(request, "dashboard/partials/trip_members_panel.html", {"trip": trip, "members": members, "profile": profile})


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
        # Creator cannot be removed; only creator can remove others
        if target == trip.creator:
            return HttpResponse("The trip creator cannot be removed.", status=400)
        if profile not in {target, trip.creator}:
            return HttpResponse("Only the trip creator can remove other members.", status=403)
        trip.profiles.remove(target)

        members = trip.profiles.select_related("user").order_by("user__username")
        return render(request, "dashboard/partials/trip_members_panel.html", {"trip": trip, "members": members, "profile": profile})


class TripLocationSearchView(LoginRequiredMixin, View):
    """JSON search for locations to add as activities.

    GET /trips/location-search/?q=<query>
    """

    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})
        from urbanlens.dashboard.models.location.model import Location
        locations = (
            Location.objects.filter(name__icontains=q)
            .values("uuid", "name", "locality", "administrative_area_level_1")[:10]
        )
        return JsonResponse({"results": list(locations)})


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

        activities = (
            trip.activities
            .select_related("location", "pin")
            .order_by("scheduled_at", "order", "created")
        )

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
                "label": label,
                "lat": float(lat),
                "lng": float(lng),
                "scheduled_at": act.scheduled_at.isoformat() if act.scheduled_at else None,
            })
            index += 1

        return JsonResponse({"points": points})
