"""Memories page controllers - map/timeline aggregation of routes, trips, visits, photos."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.models.trips.model import TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.memories.aggregator import BBox, get_memory_events

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_DAYS = 90
_ON_THIS_DAY_LIMIT = 10


def _parse_date(value: str | None, default: datetime.date) -> datetime.date:
    """Parse an ISO date query param, falling back to *default* if missing/invalid."""
    if not value:
        return default
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return default


def _parse_bbox(request: HttpRequest) -> BBox | None:
    """Parse a ``minLat,minLng,maxLat,maxLng`` bbox query param, or None if absent/invalid."""
    raw = request.GET.get("bbox")
    if not raw:
        return None
    try:
        min_lat, min_lng, max_lat, max_lng = (float(part) for part in raw.split(","))
    except ValueError:
        return None
    return BBox(min_lat, min_lng, max_lat, max_lng)


def _earliest_memory_date(profile: Profile) -> datetime.date | None:
    """Return the earliest date across all of a profile's memory sources, for scrubber bounds."""
    candidates: list[datetime.date] = []

    route_started = Route.objects.for_profile(profile).order_by("started_at").values_list("started_at", flat=True).first()
    if route_started:
        candidates.append(route_started.date())

    visit_at = PinVisit.objects.filter(pin__profile=profile).order_by("visited_at").values_list("visited_at", flat=True).first()
    if visit_at:
        candidates.append(visit_at.date())

    photo_at = Image.objects.filter(profile=profile).order_by("created").values_list("created", flat=True).first()
    if photo_at:
        candidates.append(photo_at.date())

    return min(candidates) if candidates else None


class MemoriesView(LoginRequiredMixin, View):
    """Memories page - map + timeline of everywhere a profile has been.

    GET /memories/
    """

    def get(self, request: HttpRequest):
        """Render the Memories page with hero stats and scrubber bounds.

        Args:
            request: The HTTP request.

        Returns:
            Rendered Memories page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)

        routes = Route.objects.for_profile(profile)
        total_distance_m = routes.aggregate(total=Sum("distance_meters"))["total"] or 0.0
        route_count = routes.count()
        places_visited = PinVisit.objects.filter(pin__profile=profile).values("pin_id").distinct().count()
        photo_count = Image.objects.filter(profile=profile).count()
        trip_count = TripMembership.objects.filter(profile=profile).values("trip_id").distinct().count()
        has_memory_data = any((route_count, places_visited, photo_count, trip_count))

        today = timezone.now().date()
        earliest = _earliest_memory_date(profile)

        return render(
            request,
            "dashboard/pages/memories/index.html",
            {
                "profile": profile,
                "page_name": "memories",
                "has_memory_data": has_memory_data,
                "hero_stats": {
                    "total_distance_km": round(total_distance_m / 1000, 1),
                    "places_visited": places_visited,
                    "photo_count": photo_count,
                    "trip_count": trip_count,
                    "years_active": (today.year - earliest.year + 1) if earliest else 0,
                },
                "earliest_date": earliest.isoformat() if earliest else today.isoformat(),
                "today": today.isoformat(),
                **profile.get_map_center_template_context(),
            },
        )


class MemoriesFeedDataView(LoginRequiredMixin, View):
    """Aggregated map/timeline data for the Memories page.

    GET /memories/data/?start=YYYY-MM-DD&end=YYYY-MM-DD&bbox=minLat,minLng,maxLat,maxLng

    A date range is always applied (defaulting to the trailing 90 days) so a
    profile's full history is never loaded in a single request.
    """

    def get(self, request: HttpRequest):
        """Return MemoryEvents for the requested date range/viewport as JSON.

        Args:
            request: The HTTP request.

        Returns:
            JsonResponse with ``start``, ``end``, and ``events``.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)

        today = timezone.now().date()
        default_start = today - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS)
        start = _parse_date(request.GET.get("start"), default_start)
        end = _parse_date(request.GET.get("end"), today)
        bbox = _parse_bbox(request)

        events = get_memory_events(profile, start, end, bbox=bbox)

        return JsonResponse(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "events": [
                    {
                        "type": event.type,
                        "occurred_at": event.occurred_at.isoformat(),
                        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
                        "title": event.title,
                        "subtitle": event.subtitle,
                        "latitude": event.latitude,
                        "longitude": event.longitude,
                        "url": event.url,
                        "thumbnail_url": event.thumbnail_url,
                        "icon": event.icon,
                        "color": event.color,
                        "extra": event.extra,
                    }
                    for event in events
                ],
            },
        )


class MemoriesOnThisDayView(LoginRequiredMixin, View):
    """HTMX partial - memories from this month/day in past years.

    GET /memories/on-this-day/
    """

    def get(self, request: HttpRequest):
        """Render the "on this day" callout partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered partial listing past-year visits/routes/photos on today's month/day.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        today = timezone.now().date()

        visits = list(
            PinVisit.objects.filter(pin__profile=profile, visited_at__month=today.month, visited_at__day=today.day)
            .exclude(visited_at__year=today.year)
            .select_related("pin")
            .order_by("-visited_at")[:_ON_THIS_DAY_LIMIT],
        )
        routes = list(
            Route.objects.for_profile(profile)
            .filter(started_at__month=today.month, started_at__day=today.day)
            .exclude(started_at__year=today.year)
            .order_by("-started_at")[:_ON_THIS_DAY_LIMIT],
        )
        photos = list(
            Image.objects.filter(profile=profile, taken_at__month=today.month, taken_at__day=today.day)
            .exclude(taken_at__year=today.year)
            .select_related("pin", "location")
            .order_by("-taken_at")[:_ON_THIS_DAY_LIMIT],
        )

        return render(
            request,
            "dashboard/partials/memories/_on_this_day.html",
            {"visits": visits, "routes": routes, "photos": photos, "today": today},
        )
