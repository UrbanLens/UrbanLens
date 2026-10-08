"""Extensible aggregation of a profile's "memories" - routes, trips, visits, photos.

Each source function does its own date/bbox filtering on its own model's already-indexed
fields, and contributes independently: one source failing omits its own events, never the feed.

A source's signature is ``(profile, start, end, bbox, before=None)`` and it must yield
**newest first**: the feed takes each source's first N, so an unordered source would
contribute an arbitrary N rather than its newest N. ``before`` is the exclusive page cursor -
a source that ignores it will repeat its newest events on every page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
import itertools
import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from django.db.models import Prefetch
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.models import Model, QuerySet

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip


logger = logging.getLogger(__name__)


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
        extra: Type-specific extra data the frontend may want."""

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


#: Slice size for a source read with no limit.
_DEFAULT_SLICE = 200


def _in_slices[RowT: Model](queryset: QuerySet[RowT], size: int | None) -> Iterator[RowT]:
    """Rows of an ordered queryset, fetched one ``LIMIT``/``OFFSET`` slice at a time.

    Iterating a queryset loads every row before yielding the first, so ``islice`` over a plain
    ``for row in queryset`` bounds nothing. A consumer that stops here stops the reads too.

    Args:
        queryset: Rows in a total order, so consecutive slices neither repeat nor skip.
        size: Rows per slice; the caller's limit, so an SQL-complete source costs one query.

    Yields:
        The rows, in order.
    """
    for batch in _slices(queryset, size):
        yield from batch


def _slices[RowT: Model](queryset: QuerySet[RowT], size: int | None) -> Iterator[list[RowT]]:
    """The slices :func:`_in_slices` reads, for a caller that works on a slice's rows together.

    Args:
        queryset: Rows in a total order, so consecutive slices neither repeat nor skip.
        size: Rows per slice.

    Yields:
        Each slice's rows, in order; the last may be short or empty.
    """
    size = size or _DEFAULT_SLICE
    offset = 0
    while True:
        batch = list(queryset[offset : offset + size])
        yield batch
        if len(batch) < size:
            return
        offset += size


def _date_to_datetime(value: date) -> datetime:
    """Convert a plain date to a tz-aware datetime at midnight, for feed sorting."""
    combined = datetime.combine(value, time.min)
    return timezone.make_aware(combined) if timezone.is_naive(combined) else combined


def _routes_for_range(profile: Profile, start: date, end: date, bbox: BBox | None, before: datetime | None = None, *, limit: int | None = None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each Route that started within the given range."""
    from urbanlens.dashboard.models.routes.model import Route
    from urbanlens.dashboard.services.core.units import format_distance

    units = profile.effective_distance_units
    routes = Route.objects.for_profile(profile).in_date_range(start, end).order_by("-started_at", "-pk")
    if before is not None:
        routes = routes.filter(started_at__lt=before)
    if bbox is not None:
        routes = routes.intersecting_bbox(bbox.min_lat, bbox.min_lng, bbox.max_lat, bbox.max_lng)

    for route in _in_slices(routes, limit):
        if route.started_at is None:
            continue
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


def _trip_representative_point(trip: Trip, hidden: set[int]) -> tuple[float, float] | None:
    """Return a representative (lat, lng) for a trip, from its earliest coordinate-bearing activity the viewer may see.

    A stop whose location is hidden from the viewer (*hidden*, from ``viewer_hidden_activity_ids``) is passed over:
    its point would place the trip on the map, and a ``bbox`` filter could find it.
    """
    # trip.activities.all() rather than a fresh .select_related().order_by() chain, so the
    # caller's Prefetch is actually used - re-filtering the manager would re-query per trip.
    for activity in trip.activities.all():
        if activity.id in hidden:
            continue
        if activity.lat_override is not None and activity.lng_override is not None:
            return (activity.lat_override, activity.lng_override)
        if activity.pin and activity.pin.effective_latitude is not None and activity.pin.effective_longitude is not None:
            return (float(activity.pin.effective_latitude), float(activity.pin.effective_longitude))
        if activity.location and activity.location.latitude is not None and activity.location.longitude is not None:
            return (float(activity.location.latitude), float(activity.location.longitude))
    return None


def _trips_for_range(profile: Profile, start: date, end: date, bbox: BBox | None, before: datetime | None = None, *, limit: int | None = None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each Trip whose effective date range overlaps the given range."""
    from urbanlens.dashboard.models.trips.model import Trip, TripActivity
    from urbanlens.dashboard.services.trips.trip_visibility import viewer_hidden_activity_ids

    # Mirrors Trip.effective_start_date/effective_end_date: explicit start_date/end_date win, else
    # fall back to the earliest/latest scheduled activity.
    # A trip with no end_date and no later activity is treated as ending on its effective start
    # date, same as Trip.duration_days/timeline_status do.
    trips = (
        Trip.objects.filter(pk__in=Trip.objects.filter(profiles=profile).values("pk"))
        .overlapping(start, end)
        .prefetch_related(
            Prefetch(
                "activities",
                queryset=TripActivity.objects.select_related("pin", "location", "added_by").order_by("scheduled_at", "order"),
            )
        )
        .order_by("-_eff_start", "-pk")
    )
    if before is not None:
        # A trip is day-granular, so this is the coarse half of the cursor: it
        # keeps the boundary day, and get_memory_events drops the events on it
        # that were already delivered. Everything newer is excluded here, so the
        # per-source cap is not spent on rows the caller has already seen.
        trips = trips.filter(_eff_start__lte=before.date())

    for batch in _slices(trips, limit):
        # Which stops the viewer may not see, for the whole slice at once rather than a query or two per trip.
        hidden = viewer_hidden_activity_ids([activity for trip in batch for activity in trip.activities.all()], profile)
        yield from _trip_events(batch, hidden, bbox)


def _trip_events(trips: list[Trip], hidden: set[int], bbox: BBox | None) -> Iterator[MemoryEvent]:
    """The MemoryEvents of one slice of :func:`_trips_for_range`'s trips, with the ids of the stops hidden from the viewer."""
    for trip in trips:
        # The annotations, not the equivalent model properties: those re-derive the same
        # two dates with a query apiece, which on this page is per trip in the feed.
        occurred_at = trip._eff_start  # noqa: SLF001
        if occurred_at is None:
            continue
        ended_at = trip._eff_end  # noqa: SLF001
        point = _trip_representative_point(trip, hidden)
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
            url=reverse("trips.detail", kwargs={"trip_slug": trip.slug}),
            thumbnail_url=None,
            icon="luggage",
            color="#FF9800",
            extra={"uuid": str(trip.uuid)},
        )


def _visits_for_range(profile: Profile, start: date, end: date, bbox: BBox | None, before: datetime | None = None, *, limit: int | None = None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each PinVisit within the given range."""
    from urbanlens.dashboard.models.visits.model import PinVisit

    visits = PinVisit.objects.filter(pin__profile=profile, visited_at__date__range=(start, end)).select_related("pin__location__wiki").order_by("-visited_at", "-pk")
    if before is not None:
        visits = visits.filter(visited_at__lt=before)
    if bbox is not None:
        visits = visits.filter(
            pin__location__latitude__range=(bbox.min_lat, bbox.max_lat),
            pin__location__longitude__range=(bbox.min_lng, bbox.max_lng),
        )

    for visit in _in_slices(visits, limit):
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
            extra={"source": visit.source, "pin_slug": pin.slug, "visit_id": visit.pk},
        )


def _photos_for_range(profile: Profile, start: date, end: date, bbox: BBox | None, before: datetime | None = None, *, limit: int | None = None) -> Iterator[MemoryEvent]:
    """Yield a MemoryEvent for each of the profile's own geotagged photos within the given range."""
    from urbanlens.dashboard.models.images.model import Image

    # Named `_effective_taken_at`, not `effective_taken_at`: annotating the
    # latter collides with Image.effective_taken_at (a real @property), and
    # Django raises AttributeError trying to setattr the annotated column
    # onto that name during row materialization - silently swallowed by
    # get_memory_events' broad exception guard, so every photo vanished from
    # the feed. The Coalesce fallback matches the property's own chain
    # (EXIF taken_at, else a filename-parsed date) so the two stay in sync;
    # reading the real property below (not this annotation) keeps that the
    # single source of truth.
    photos = (
        Image.objects.filter(profile=profile)
        .with_coords()
        .servable()
        .annotate(_effective_taken_at=Coalesce("taken_at", "filename_taken_at"))
        .filter(_effective_taken_at__date__range=(start, end))
        .select_related("pin", "wiki", "wiki__location")
        .order_by("-_effective_taken_at", "-pk")
    )
    if before is not None:
        photos = photos.filter(_effective_taken_at__lt=before)
    if bbox is not None:
        photos = photos.filter(
            latitude__range=(bbox.min_lat, bbox.max_lat),
            longitude__range=(bbox.min_lng, bbox.max_lng),
        )

    for image in _in_slices(photos, limit):
        taken_at = image.effective_taken_at
        if taken_at is None or image.latitude is None or image.longitude is None:
            continue
        target = image.pin or image.wiki
        url = ""
        if image.pin:
            url = reverse("pin.details", kwargs={"pin_slug": image.pin.slug})
        elif image.wiki and image.wiki.location and image.wiki.location.slug:
            url = reverse("location.wiki", kwargs={"location_slug": image.wiki.location.slug})

        subtitle = ""
        if target is not None:
            subtitle = target.effective_name if hasattr(target, "effective_name") else getattr(target, "name", "")

        yield MemoryEvent(
            type="photo",
            occurred_at=taken_at,
            ended_at=None,
            title=image.caption or "Photo",
            subtitle=subtitle,
            latitude=float(image.latitude),
            longitude=float(image.longitude),
            url=url,
            thumbnail_url=image.file_url,
            icon="photo_camera",
            color="#E91E63",
            extra={"image_id": image.pk},
        )


def _event_sources() -> tuple[Callable[..., Iterator[MemoryEvent]], ...]:
    """The registered memory sources, resolved fresh on each call.

    A module-level tuple would capture these at import time, which both hides
    monkeypatching from tests and quietly defeats any later attempt to swap a source.
    """
    return (
        _routes_for_range,
        _trips_for_range,
        _visits_for_range,
        _photos_for_range,
    )


def get_memory_events(profile: Profile, start: date, end: date, *, bbox: BBox | None = None, limit: int | None = None, before: datetime | None = None) -> list[MemoryEvent]:
    """Merge every registered event source over [start, end], sorted newest-first.

    Args:
        profile: The profile whose memories to fetch.
        start: Earliest date (inclusive).
        end: Latest date (inclusive).
        bbox: Optional map-viewport bounding box to further narrow results.
        limit: The most events to return. Each source is also stopped at this
            many, so the merge is bounded whatever date range was asked for -
            which is the point, since the range comes from the client. Correct
            only because every source yields newest-first; taking the first N of
            an unordered source would keep an arbitrary N.
        before: Exclusive cursor - only events strictly older than this. A date
            would not do: a single day holding more events than *limit* would
            return the same page forever, because the cursor could never move
            past it. Known limit: events sharing an exact timestamp cannot be
            split across pages, so more than *limit* of them at one instant
            loses the overflow rather than looping. Real timestamps collide
            rarely enough that a composite cursor has not been worth its cost.

    Returns:
        List of MemoryEvent across every source that succeeded, newest first, at
        most *limit* of them.
    """
    events: list[MemoryEvent] = []
    for source in _event_sources():
        try:
            # islice() consumes the generator incrementally and stops early, so a
            # source that raises partway keeps whatever it already yielded, and a
            # source with a million rows is not drained to find the newest few.
            stream = source(profile, start, end, bbox, before, limit=limit)
            events.extend(itertools.islice(stream, limit) if limit is not None else stream)
        except Exception:
            logger.exception("Memory source %s failed; omitting it from the feed", getattr(source, "__name__", source))
    if before is not None:
        # The exact half of the cursor. Day-granular sources filter to the
        # boundary day and no further, so this is what removes the events on
        # that day the caller already has.
        events = [event for event in events if event.occurred_at < before]
    events.sort(key=lambda e: e.occurred_at, reverse=True)
    return events[:limit] if limit is not None else events
