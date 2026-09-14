"""Turn a device's raw scan history into fuzzy WikiDeviceMarker location(s)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Sequence
    import datetime

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, ScannedDevice, WikiDeviceMarker
    from urbanlens.dashboard.models.wiki.model import Wiki

#: How far back scan history is considered at all. Contributions older than
#: this simply drop out of every recompute, same as if they'd never existed.
LOOKBACK_DAYS = 720

#: Recency half-life for an entry's clustering weight - a 30-day-old observation counts for half as
#: much as a fresh one, matching how quickly a transient setup (a contractor's temporary camera)
#: should stop dominating a marker's position over a permanent one.
DECAY_HALF_LIFE_DAYS = 90

#: Entries within this real-world distance of each other (or of a marker)
#: are treated as the same physical device location.
MERGE_DISTANCE_METERS = 30.0

#: Floor on a marker's fuzzy radius - even a single, highly-weighted
#: observation shouldn't collapse to an implausibly precise point.
MIN_RADIUS_METERS = 5.0

#: tuned so a couple of independent, recent corroborating scans already read
#: as meaningfully confident, while a single one stays low.
CONFIDENCE_SATURATION_WEIGHT = 3.0

#: Consecutive "not detected" reports (with no positive detection in
#: between) after which a marker is presumed removed.
ABSENCE_STREAK_THRESHOLD = 10

_EARTH_RADIUS_METERS = 6_371_000.0


def _haversine_meters(a: Point, b: Point) -> float:
    """Great-circle distance in meters between two WGS-84 points."""
    lat1, lng1 = math.radians(a.y), math.radians(a.x)
    lat2, lng2 = math.radians(b.y), math.radians(b.x)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return _EARTH_RADIUS_METERS * 2 * math.asin(math.sqrt(h))


def weight_for_age(age: timedelta) -> float:
    """Recency weight for an observation this old - halves every ``DECAY_HALF_LIFE_DAYS``.

    Args:
        age: How long ago the observation was made.

    Returns:
        A weight in ``(0, 1]`` - always positive, never exceeding 1."""
    age_days = max(age.total_seconds() / 86_400, 0.0)
    return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)


def confidence_for_weight(total_weight: float) -> float:
    """Saturating confidence score for a cluster's total accumulated weight.

    Args:
        total_weight: Sum of every contributing entry's recency weight.

    Returns:
        A value in ``[0, 1)`` that increases monotonically with weight but never reaches 1 - there is always room for one more corroborating scan."""
    return 1.0 - math.exp(-max(total_weight, 0.0) / CONFIDENCE_SATURATION_WEIGHT)


def weighted_centroid(points_with_weights: Sequence[tuple[Point, float]]) -> Point:
    """Weighted average position of a set of points.

    Args:
        points_with_weights: Non-empty sequence of (point, weight) pairs.

    Returns:
        The weighted-average point, SRID 4326."""
    from django.contrib.gis.geos import Point

    total_weight = sum(weight for _, weight in points_with_weights)
    avg_lat = sum(point.y * weight for point, weight in points_with_weights) / total_weight
    avg_lng = sum(point.x * weight for point, weight in points_with_weights) / total_weight
    return Point(avg_lng, avg_lat, srid=4326)


def weighted_radius_meters(points_with_weights: Sequence[tuple[Point, float]], centroid: Point) -> float:
    """Weighted RMS spread (in meters) of a set of points around their centroid.

    Args:
        points_with_weights: Non-empty sequence of (point, weight) pairs.
        centroid: The points' weighted centroid.

    Returns:
        The weighted RMS distance from centroid, floored at ``MIN_RADIUS_METERS``."""
    total_weight = sum(weight for _, weight in points_with_weights)
    variance = sum(weight * _haversine_meters(point, centroid) ** 2 for point, weight in points_with_weights) / total_weight
    return max(math.sqrt(variance), MIN_RADIUS_METERS)


@dataclass(slots=True)
class _WeightedEntry:
    """One DeviceScanEntry, pre-computed for clustering."""

    point: Point
    weight: float
    observed_at: datetime.datetime
    avg_signal_strength: float | None


def _weight_entry(entry: DeviceScanEntry, now: datetime.datetime) -> _WeightedEntry:
    """Build a :class:`_WeightedEntry` from a persisted scan entry.

    Args:
        entry: A ``detected=True`` scan entry, with its ``readings`` prefetched.
        now: Reference time for the recency-weight calculation.

    Returns:
        The entry's clustering inputs."""
    readings = list(entry.readings.all())
    signal_values = [reading.signal_strength for reading in readings if reading.signal_strength is not None]
    avg_signal = sum(signal_values) / len(signal_values) if signal_values else None
    # The client-submitted observation time, not row-insertion time: an offline mobile client can
    # upload readings well after they were taken, and entry.created would then treat stale scans as
    # fresh.
    # Falls back to entry.created only for the edge case of a detected=True entry with no readings
    observed_at = max((reading.observed_at for reading in readings), default=entry.created)
    return _WeightedEntry(
        point=entry.location,
        weight=weight_for_age(now - observed_at),
        observed_at=observed_at,
        avg_signal_strength=avg_signal,
    )


def _cluster_entries(entries: Sequence[_WeightedEntry]) -> list[list[_WeightedEntry]]:
    """Greedily group entries within ``MERGE_DISTANCE_METERS`` of a growing cluster centroid.
    Heaviest (most recent/most corroborated) entries seed clusters first, so a cluster's running centroid stabilizes quickly.

    Args:
        entries: Weighted entries to group.

    Returns:
        Clusters (each a non-empty list of entries), in no particular order."""
    remaining = sorted(entries, key=lambda e: e.weight, reverse=True)
    clusters: list[list[_WeightedEntry]] = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        centroid = seed.point
        changed = True
        while changed:
            changed = False
            still_remaining = []
            for candidate in remaining:
                if _haversine_meters(candidate.point, centroid) <= MERGE_DISTANCE_METERS:
                    cluster.append(candidate)
                    changed = True
                else:
                    still_remaining.append(candidate)
            remaining = still_remaining
            if changed:
                centroid = weighted_centroid([(e.point, e.weight) for e in cluster])
        clusters.append(cluster)
    return clusters


def _match_existing_marker(candidates: Sequence[WikiDeviceMarker], centroid: Point, already_matched: set[int]) -> WikiDeviceMarker | None:
    """The nearest not-yet-matched marker within ``MERGE_DISTANCE_METERS`` of *centroid*, if any."""
    best: WikiDeviceMarker | None = None
    best_distance = MERGE_DISTANCE_METERS
    for marker in candidates:
        if marker.pk in already_matched:
            continue
        distance = _haversine_meters(marker.centroid, centroid)
        if distance <= best_distance:
            best = marker
            best_distance = distance
    return best


def recompute_wiki_device_markers(device: ScannedDevice, wiki: Wiki) -> list[WikiDeviceMarker]:
    """Recompute this (device, wiki) pair's fuzzy marker(s) from scratch.

    Args:
        device: The device whose markers on this wiki are being recomputed.
        wiki: The wiki whose device markers are being recomputed.

    Returns:
        The markers now current for this (device, wiki) pair (created or updated this call) - callers don't need this return value for the pipeline to work, it's provided for tests."""
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
    from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, MarkerStatus, WikiDeviceMarker

    # Effective boundary (community-drawn override, else generated, else circle fallback) rather
    # than the stricter location-default-only check services.device_scan.wiki_lookup uses to *route*
    # a new detection to a wiki - this call only decides membership within a wiki its device already
    # legitimately touched, not visibility, so the broader/more accurate boundary is the right one
    polygon = Boundary.objects.effective_polygon_for_wiki(wiki, BoundaryType.PROPERTY)
    if polygon is None:
        return []

    now = timezone.now()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    entries = list(
        DeviceScanEntry.objects.filter(device=device, detected=True, location__within=polygon, created__gte=cutoff).prefetch_related("readings"),
    )
    weighted_entries = [_weight_entry(entry, now) for entry in entries]
    clusters = _cluster_entries(weighted_entries) if weighted_entries else []

    existing_markers = list(WikiDeviceMarker.objects.for_wiki_and_device(wiki, device).filter(manually_placed=False))
    matched_marker_ids: set[int] = set()
    result_markers: list[WikiDeviceMarker] = []

    for cluster in clusters:
        points_with_weights = [(e.point, e.weight) for e in cluster]
        centroid = weighted_centroid(points_with_weights)
        radius = weighted_radius_meters(points_with_weights, centroid)
        confidence = confidence_for_weight(sum(e.weight for e in cluster))
        signal_values = [e.avg_signal_strength for e in cluster if e.avg_signal_strength is not None]
        avg_signal = sum(signal_values) / len(signal_values) if signal_values else None

        marker = _match_existing_marker(existing_markers, centroid, matched_marker_ids)
        if marker is not None:
            matched_marker_ids.add(marker.pk)
            marker.centroid = centroid
            marker.radius_meters = radius
            marker.confidence = confidence
            marker.observation_count = len(cluster)
            marker.avg_signal_strength = avg_signal
            marker.absence_streak = 0
            marker.first_observed_at = min(marker.first_observed_at, *(e.observed_at for e in cluster))
            marker.last_observed_at = max(e.observed_at for e in cluster)
            if marker.status in (MarkerStatus.STALE, MarkerStatus.PRESUMED_REMOVED):
                marker.status = MarkerStatus.ACTIVE
            marker.save()
        else:
            marker = WikiDeviceMarker.objects.create(
                wiki=wiki,
                device=device,
                status=MarkerStatus.ACTIVE,
                centroid=centroid,
                radius_meters=radius,
                confidence=confidence,
                observation_count=len(cluster),
                avg_signal_strength=avg_signal,
                first_observed_at=min(e.observed_at for e in cluster),
                last_observed_at=max(e.observed_at for e in cluster),
            )
        result_markers.append(marker)

    for marker in existing_markers:
        if marker.pk not in matched_marker_ids and marker.status == MarkerStatus.ACTIVE:
            marker.status = MarkerStatus.STALE
            marker.save(update_fields=["status", "updated"])

    return result_markers


def record_absence_report(marker: WikiDeviceMarker) -> WikiDeviceMarker:
    """Apply one "expected device not found here" report to *marker*.
    The increment is an ``F`` expression rather than a read-modify-write.

    Args:
        marker: The marker a client reported not detecting.

    Returns:
        The marker, refreshed to the stored counter and status."""
    from django.db.models import F

    from urbanlens.dashboard.models.device_scan.model import MarkerStatus, WikiDeviceMarker as MarkerModel

    now = timezone.now()
    MarkerModel.objects.filter(pk=marker.pk).update(absence_streak=F("absence_streak") + 1, updated=now)
    marker.refresh_from_db(fields=["absence_streak", "status", "updated"])

    if marker.absence_streak >= ABSENCE_STREAK_THRESHOLD and marker.status != MarkerStatus.PRESUMED_REMOVED:
        MarkerModel.objects.filter(pk=marker.pk).update(status=MarkerStatus.PRESUMED_REMOVED, updated=now)
        marker.status = MarkerStatus.PRESUMED_REMOVED
    return marker
