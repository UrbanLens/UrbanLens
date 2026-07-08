"""Extensible aggregation of a profile's "memories" - routes, trips, visits, photos.

Adding a future memory type is one new ``_x_for_range`` function appended to
``_EVENT_SOURCES`` below - nothing else needs to change. Each source function
does its own date/bbox filtering on its own model's already-indexed fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, NamedTuple

from django.db.models import DateField, Max, Min
from django.db.models.functions import Cast, Coalesce
from django.urls import reverse
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip


class BBox(NamedTuple):
    """A lat/lng viewport bounding box used to scope map-visible memories."""

    min_lat: float
    min_lng: float
    max_lat: float
    max_lng: float


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    """One row in the unified Memories feed - the extensibility seam for new memory types.

    Attributes:
        type: One of "route", "trip", "visit", "photo".
        occurred_at: When this memory happened (tz-aware).
        ended_at: When it ended, if it spans a range (e.g. a route or trip).
        title: Short display title.
        subtitle: Secondary display text (e.g. distance, visit source).
        latitude: Representative point latitude, if any.
        longitude: Representative point longitude, if any.
        url: Link to the relevant detail page, or "" if none exists.
        thumbnail_url: A representative photo URL, if any.
        icon: Material icon name for the map marker/card.
        color: Hex color for the map marker/card accent.
        extra: Type-specific extra data the frontend may want.
    """

    type: str
    occurred_at: datetime
    ended_at: datetime | None
    title: str
    subtitle: str
    latitude: float | None
    longitude: float | None
    url: str
    thumbnail_url: str | None
    icon: str
    color: str
    extra: dict[str, Any] = field(default_factory=dict)


def _date_to_datetime(value: date) -> datetime:
    """Convert a plain date to a tz-aware datetime at midnight, for feed sorting."""
    combined = datetime.combine(value, time.min)
    return timezone.make_aware(combined) if timezone.is_naive(combined) else combined


def _routes_for_range(profile: Profile, start: date, end: date, bbox: BBox | None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each Route that started within the given range."""
    from urbanlens.dashboard.models.routes.model import Route
    from urbanlens.dashboard.services.units import format_distance

    units = profile.effective_distance_units
    routes = Route.objects.for_profile(profile).in_date_range(start, end)
    if bbox is not None:
        routes = routes.intersecting_bbox(bbox.min_lat, bbox.min_lng, bbox.max_lat, bbox.max_lng)

    for route in routes:
        start_lng, start_lat = route.path.coords[0]
        distance_km = route.distance_meters / 1000
        yield MemoryEvent(
            type="route",
            occurred_at=route.started_at,
            ended_at=route.ended_at,
            title=route.name or route.get_source_display(),
            subtitle=format_distance(distance_km, units),
            latitude=start_lat,
            longitude=start_lng,
            url="",
            thumbnail_url=None,
            icon="route",
            color="#2196F3",
            extra={
                "uuid": str(route.uuid),
                "distance_meters": route.distance_meters,
                "elevation_gain_meters": route.elevation_gain_meters,
                "source": route.source,
                "path_geojson": route.path.geojson,
            },
        )


def _trip_representative_point(trip: Trip) -> tuple[float, float] | None:
    """Return a representative (lat, lng) for a trip, from its earliest coordinate-bearing activity.

    Mirrors the override priority used for trip map markers elsewhere
    (lat_override/lng_override -> pin's effective coords -> location coords).
    """
    activities = trip.activities.select_related("pin", "location").order_by("scheduled_at", "order")
    for activity in activities:
        if activity.lat_override is not None and activity.lng_override is not None:
            return (activity.lat_override, activity.lng_override)
        if activity.pin and activity.pin.effective_latitude is not None and activity.pin.effective_longitude is not None:
            return (float(activity.pin.effective_latitude), float(activity.pin.effective_longitude))
        if activity.location and activity.location.latitude is not None and activity.location.longitude is not None:
            return (float(activity.location.latitude), float(activity.location.longitude))
    return None


def _trips_for_range(profile: Profile, start: date, end: date, bbox: BBox | None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each Trip whose effective date range overlaps the given range."""
    from urbanlens.dashboard.models.trips.model import Trip

    # Mirrors Trip.effective_start_date/effective_end_date: explicit start_date/end_date
    # win, else fall back to the earliest/latest scheduled activity. A trip with no
    # end_date and no later activity is treated as ending on its effective start date,
    # same as Trip.duration_days/timeline_status do.
    trips = (
        Trip.objects.filter(profiles=profile)
        .annotate(
            _first_activity_date=Cast(Min("activities__scheduled_at"), output_field=DateField()),
            _last_activity_date=Cast(Max("activities__scheduled_at"), output_field=DateField()),
        )
        .annotate(_eff_start=Coalesce("start_date", "_first_activity_date"))
        .annotate(_eff_end=Coalesce("end_date", "_last_activity_date", "_eff_start"))
        .filter(_eff_start__isnull=False, _eff_start__lte=end, _eff_end__gte=start)
        .distinct()
    )

    for trip in trips:
        occurred_at = trip.effective_start_date
        if occurred_at is None:
            continue
        ended_at = trip.effective_end_date
        point = _trip_representative_point(trip)
        if bbox is not None and (point is None or not (bbox.min_lat <= point[0] <= bbox.max_lat and bbox.min_lng <= point[1] <= bbox.max_lng)):
            continue
        yield MemoryEvent(
            type="trip",
            occurred_at=_date_to_datetime(occurred_at),
            ended_at=_date_to_datetime(ended_at) if ended_at else None,
            title=trip.name,
            subtitle="Trip",
            latitude=point[0] if point else None,
            longitude=point[1] if point else None,
            url=reverse("trips.detail", kwargs={"trip_uuid": trip.uuid}),
            thumbnail_url=None,
            icon="luggage",
            color="#FF9800",
            extra={"uuid": str(trip.uuid)},
        )


def _visits_for_range(profile: Profile, start: date, end: date, bbox: BBox | None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each PinVisit within the given range."""
    from urbanlens.dashboard.models.visits.model import PinVisit

    visits = PinVisit.objects.filter(pin__profile=profile, visited_at__date__range=(start, end)).select_related("pin")
    if bbox is not None:
        visits = visits.filter(
            pin__latitude__range=(bbox.min_lat, bbox.max_lat),
            pin__longitude__range=(bbox.min_lng, bbox.max_lng),
        )

    for visit in visits:
        pin = visit.pin
        yield MemoryEvent(
            type="visit",
            occurred_at=visit.visited_at,
            ended_at=None,
            title=pin.effective_name,
            subtitle=f"Visit ({visit.get_source_display()})",
            latitude=pin.effective_latitude,
            longitude=pin.effective_longitude,
            url=reverse("pin.details", kwargs={"pin_slug": pin.slug}),
            thumbnail_url=None,
            icon="pin_drop",
            color="#4CAF50",
            extra={"source": visit.source, "pin_slug": pin.slug},
        )


def _photos_for_range(profile: Profile, start: date, end: date, bbox: BBox | None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each of the profile's own geotagged photos within the given range."""
    from urbanlens.dashboard.models.images.model import Image

    photos = Image.objects.filter(profile=profile).with_coords().annotate(effective_taken_at=Coalesce("taken_at", "created")).filter(effective_taken_at__date__range=(start, end)).select_related("pin", "wiki", "wiki__location")
    if bbox is not None:
        photos = photos.filter(
            latitude__range=(bbox.min_lat, bbox.max_lat),
            longitude__range=(bbox.min_lng, bbox.max_lng),
        )

    for image in photos:
        target = image.pin or image.wiki
        url = ""
        if image.pin:
            url = reverse("pin.gallery", kwargs={"pin_slug": image.pin.slug})
        elif image.wiki and image.wiki.location and image.wiki.location.slug:
            url = reverse("location.wiki.gallery", kwargs={"location_slug": image.wiki.location.slug})

        subtitle = ""
        if target is not None:
            subtitle = target.effective_name if hasattr(target, "effective_name") else getattr(target, "name", "")

        yield MemoryEvent(
            type="photo",
            occurred_at=image.effective_taken_at,
            ended_at=None,
            title=image.caption or "Photo",
            subtitle=subtitle,
            latitude=float(image.latitude),
            longitude=float(image.longitude),
            url=url,
            thumbnail_url=image.image.url if image.image else None,
            icon="photo_camera",
            color="#E91E63",
            extra={"image_id": image.pk},
        )


_EVENT_SOURCES: tuple[Callable[[Profile, date, date, BBox | None], Iterator[MemoryEvent]], ...] = (
    _routes_for_range,
    _trips_for_range,
    _visits_for_range,
    _photos_for_range,
)


def get_memory_events(profile: Profile, start: date, end: date, *, bbox: BBox | None = None) -> list[MemoryEvent]:
    """Merge every registered event source over [start, end], sorted newest-first.

    Args:
        profile: The profile whose memories to fetch.
        start: Earliest date (inclusive).
        end: Latest date (inclusive).
        bbox: Optional map-viewport bounding box to further narrow results.

    Returns:
        List of MemoryEvent across all sources, newest first.
    """
    events: list[MemoryEvent] = []
    for source in _EVENT_SOURCES:
        events.extend(source(profile, start, end, bbox))
    events.sort(key=lambda e: e.occurred_at, reverse=True)
    return events
