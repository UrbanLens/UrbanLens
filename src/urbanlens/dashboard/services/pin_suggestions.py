"""Batch photo-location ingestion: matches scanned photo/asset coordinates against a
profile's existing pins and clusters whatever doesn't match into new-pin suggestions.

Shared by two entry points that both ultimately produce the same shape of data - a
list of (latitude, longitude, taken_at) hits - and feed it through the same pipeline:

- ``tasks.sweep_immich_library_locations``: a full sweep of a user's Immich library.
- ``controllers.tools.PhotoLocationScanUploadView``: the client-side local-folder
  scanner on the Tools page, which already clusters/dedupes in the browser before
  uploading, so its hits arrive pre-grouped.

Neither path ever creates a Pin or PinVisit directly - matching/clustering only
produces or updates ``PinSuggestion`` rows; ``accept_pin_suggestion`` is the only
function here that writes a Pin or PinVisit, and only when the user explicitly
accepts one.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.boundary.queryset import DEFAULT_RADIUS_METERS
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import MAX_STORED_VISIT_DATES, PinSuggestion, PinSuggestionStatus
from urbanlens.dashboard.models.profile.model import _haversine_km
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits import add_visited_status, find_pin_containing_point, resolve_location_for_point, sync_last_visited, visit_logging_allowed

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestionOrigin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Merge threshold for both clustering unmatched hits into a new-pin candidate and
#: matching a hit against an existing pending new-pin suggestion. Deliberately the
#: same radius as a pin's own default circle boundary (``DEFAULT_RADIUS_METERS``) -
#: a new-pin cluster's implicit footprint matches what the pin would already treat
#: as "here" once it exists.
CLUSTER_RADIUS_M = DEFAULT_RADIUS_METERS


@dataclass(frozen=True, slots=True)
class LocationHit:
    """One geotagged, dated data point discovered by a batch scan.

    Attributes:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        taken_at: When the source photo/asset was captured.
        label: Optional place-name hint (e.g. Immich's reverse-geocoded city),
            offered as ``PinSuggestion.suggested_name`` for new-pin clusters.
    """

    latitude: float
    longitude: float
    taken_at: datetime.datetime
    label: str | None = None


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """Counts returned to the caller for a progress/toast message."""

    matched_suggestions: int
    new_pin_suggestions: int
    hits_processed: int


def _dates_from_hits(hits: list[LocationHit]) -> list[str]:
    """Return the distinct, sorted, capped ISO dates among a list of hits."""
    return sorted({hit.taken_at.date().isoformat() for hit in hits})[:MAX_STORED_VISIT_DATES]


def _merge_dates(existing: list[str], new: list[str]) -> list[str]:
    """Return the union of two date-string lists, sorted and capped."""
    return sorted(set(existing) | set(new))[:MAX_STORED_VISIT_DATES]


def _centroid(hits: list[LocationHit]) -> tuple[float, float]:
    """Return the mean (latitude, longitude) of a list of hits."""
    count = len(hits)
    return sum(hit.latitude for hit in hits) / count, sum(hit.longitude for hit in hits) / count


def _label_from_hits(hits: list[LocationHit]) -> str:
    """Return the first non-empty hit label, or an empty string."""
    for hit in hits:
        if hit.label:
            return hit.label
    return ""


def _match_hits_to_pins(profile: Profile, hits: list[LocationHit]) -> tuple[dict[Pin, list[LocationHit]], list[LocationHit]]:
    """Split hits into ones that fall inside an existing pin's boundary and ones that don't.

    Precomputes each of the profile's root pins' effective property boundary
    once (the same polygon-then-circle-fallback resolution ``find_pin_containing_point``
    uses for a single point), then tests every hit against those polygons in memory -
    this keeps a full-library sweep from re-resolving the same boundary via extra
    DB queries once per hit.

    Args:
        profile: Owner whose pins are being matched against.
        hits: Candidate hits to classify.

    Returns:
        Tuple of (mapping of matched pin to its hits, list of unmatched hits).
    """
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    pins = list(Pin.objects.filter(profile=profile).root_pins().select_related("location"))
    pin_polygons = [(pin, Boundary.objects.effective_polygon_for_pin(pin, BoundaryType.PROPERTY)) for pin in pins]

    matched: dict[Pin, list[LocationHit]] = {}
    unmatched: list[LocationHit] = []
    for hit in hits:
        point = Point(hit.longitude, hit.latitude, srid=4326)
        matched_pin: Pin | None = None
        for pin, polygon in pin_polygons:
            if polygon is not None:
                if polygon.contains(point):
                    matched_pin = pin
                    break
            elif find_pin_containing_point(profile, point, pins=[pin]) is not None:
                # A pin with no resolvable polygon at all (e.g. no location) -
                # rare, but fall back to the same defensive check used for a
                # single live geolocation ping.
                matched_pin = pin
                break
        if matched_pin is not None:
            matched.setdefault(matched_pin, []).append(hit)
        else:
            unmatched.append(hit)
    return matched, unmatched


def _cluster_hits(hits: list[LocationHit], radius_m: float) -> list[list[LocationHit]]:
    """Greedily group hits into clusters no farther than radius_m from a running centroid.

    Args:
        hits: Unmatched hits to cluster.
        radius_m: Merge distance in metres.

    Returns:
        List of hit groups, each destined to become one new-pin suggestion.
    """
    clusters: list[list[LocationHit]] = []
    centroids: list[tuple[float, float]] = []
    for hit in hits:
        for index, centroid in enumerate(centroids):
            if _haversine_km(centroid, (hit.latitude, hit.longitude)) * 1000 <= radius_m:
                clusters[index].append(hit)
                clat, clon = centroid
                n = len(clusters[index])
                centroids[index] = (clat + (hit.latitude - clat) / n, clon + (hit.longitude - clon) / n)
                break
        else:
            clusters.append([hit])
            centroids.append((hit.latitude, hit.longitude))
    return clusters


def _find_nearby_pending_new_pin_suggestion(profile: Profile, latitude: float, longitude: float) -> PinSuggestion | None:
    """Return a pending, not-yet-matched-to-a-pin suggestion within cluster range of a point, if any."""
    candidates = PinSuggestion.objects.filter(profile=profile, pin__isnull=True, status=PinSuggestionStatus.PENDING)
    for candidate in candidates:
        point = (float(candidate.latitude), float(candidate.longitude))
        if _haversine_km(point, (latitude, longitude)) * 1000 <= CLUSTER_RADIUS_M:
            return candidate
    return None


def _upsert_matched_suggestion(profile: Profile, pin: Pin, hits: list[LocationHit], origin: PinSuggestionOrigin) -> None:
    """Create or extend the pending suggestion to log visit(s) on an existing pin."""
    dates = _dates_from_hits(hits)
    existing = PinSuggestion.objects.filter(profile=profile, pin=pin, status=PinSuggestionStatus.PENDING).first()
    if existing is not None:
        existing.visit_dates = _merge_dates(existing.visit_dates, dates)
        existing.hit_count += len(hits)
        existing.save(update_fields=["visit_dates", "hit_count", "updated"])
        return
    PinSuggestion.objects.create(
        profile=profile,
        pin=pin,
        latitude=pin.effective_latitude,
        longitude=pin.effective_longitude,
        origin=origin,
        visit_dates=dates,
        hit_count=len(hits),
    )


def _upsert_new_pin_suggestion(profile: Profile, cluster: list[LocationHit], origin: PinSuggestionOrigin) -> None:
    """Create or extend the pending suggestion to create a new pin for a cluster of hits."""
    latitude, longitude = _centroid(cluster)
    dates = _dates_from_hits(cluster)
    existing = _find_nearby_pending_new_pin_suggestion(profile, latitude, longitude)
    if existing is not None:
        existing.visit_dates = _merge_dates(existing.visit_dates, dates)
        existing.hit_count += len(cluster)
        if not existing.suggested_name:
            existing.suggested_name = _label_from_hits(cluster)
        existing.save(update_fields=["visit_dates", "hit_count", "suggested_name", "updated"])
        return
    PinSuggestion.objects.create(
        profile=profile,
        pin=None,
        latitude=latitude,
        longitude=longitude,
        origin=origin,
        visit_dates=dates,
        hit_count=len(cluster),
        suggested_name=_label_from_hits(cluster),
    )


def ingest_location_hits(profile: Profile, hits: Iterable[LocationHit], origin: PinSuggestionOrigin) -> IngestSummary:
    """Match/cluster a batch of location hits into PinSuggestion rows.

    Re-running this with overlapping hits (e.g. a repeated Immich sweep, or
    uploading local-scan results twice) merges into existing pending
    suggestions rather than creating duplicates.

    Args:
        profile: Owner the hits belong to.
        hits: Discovered (latitude, longitude, taken_at) data points.
        origin: Which batch scan produced these hits.

    Returns:
        Summary counts for the calling task/view to report.
    """
    hit_list = list(hits)
    matched, unmatched = _match_hits_to_pins(profile, hit_list)
    for pin, pin_hits in matched.items():
        _upsert_matched_suggestion(profile, pin, pin_hits, origin)

    clusters = _cluster_hits(unmatched, CLUSTER_RADIUS_M)
    for cluster in clusters:
        _upsert_new_pin_suggestion(profile, cluster, origin)

    return IngestSummary(matched_suggestions=len(matched), new_pin_suggestions=len(clusters), hits_processed=len(hit_list))


def accept_pin_suggestion(suggestion: PinSuggestion, profile: Profile) -> tuple[Pin, list[PinVisit]]:
    """Accept a pending PinSuggestion: reuse/create its pin and log any missing dated visits.

    Mirrors ``services.memories.photos.create_pin_and_log_visit`` for the new-pin
    case (resolve Location, reuse-or-create a minimal Pin, apply the suggested
    name only if the pin has none yet) and ``services.visits.accept_visit_suggestion``
    for the visit-logging case, except one ``PinVisit`` is created per distinct
    date in ``suggestion.visit_dates`` rather than a single one - a batch scan
    commonly finds several separate visits to the same place.

    Args:
        suggestion: The pending suggestion being accepted.
        profile: The accepting profile (must be suggestion.profile).

    Returns:
        Tuple of (the pin, newly-created PinVisit rows - possibly empty if
        every date was already logged, or if the profile has turned off
        visit-history tracking).
    """
    if suggestion.pin_id is not None:
        matched_pin = suggestion.pin
        if matched_pin is None:
            raise ValueError(f"PinSuggestion {suggestion.pk} has pin_id set but no matching Pin")
        pin = matched_pin
    else:
        location = resolve_location_for_point(suggestion.latitude, suggestion.longitude)
        pin = Pin.objects.filter(profile=profile, location=location, parent_pin__isnull=True).select_related("location").first()
        if pin is None:
            pin = Pin.objects.create(profile=profile, location=location)
        if suggestion.suggested_name and pin.name is None:
            pin.name = suggestion.suggested_name
            pin.name_is_user_provided = True
            pin.save(update_fields=["name", "name_is_user_provided", "updated"])
        suggestion.location = location

    visits: list[PinVisit] = []
    if visit_logging_allowed(profile):
        for date_str in suggestion.visit_dates:
            day = datetime.date.fromisoformat(date_str)
            if pin.visit_history.filter(visited_at__date=day).exists():
                continue
            visited_at = datetime.datetime.combine(day, datetime.time(12, 0), tzinfo=datetime.UTC)
            visits.append(PinVisit.objects.create(pin=pin, visited_at=visited_at, source=VisitSource.HISTORY))
        if visits:
            sync_last_visited(pin)
            add_visited_status(pin)

    suggestion.pin = pin
    suggestion.status = PinSuggestionStatus.ACCEPTED
    suggestion.save(update_fields=["pin", "location", "status", "updated"])
    return pin, visits


def reject_pin_suggestion(suggestion: PinSuggestion) -> None:
    """Reject a pending PinSuggestion.

    Args:
        suggestion: The pending suggestion being rejected.
    """
    suggestion.status = PinSuggestionStatus.REJECTED
    suggestion.save(update_fields=["status", "updated"])
