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

from dataclasses import dataclass, field
import datetime
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.boundary.queryset import DEFAULT_RADIUS_METERS
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import MAX_STORED_VISIT_DATES, MAX_SUGGESTION_PHOTOS, PinSuggestion, PinSuggestionStatus
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
        asset_id: Immich asset id this hit came from, if any - collected into
            ``PinSuggestion.sample_assets`` for review-queue thumbnails/import.
        source_key: Client-supplied cluster id (local-scan uploads only), used
            only to report back which ``PinSuggestion`` a submitted cluster
            resolved to (see ``IngestSummary.suggestion_ids_by_key``). Never
            persisted.
        weight: How many source photos this one hit stands in for - matching
            and clustering only need one representative point per distinct
            location (see ``controllers.tools._parse_cluster``, which used to
            expand a local-scan cluster's ``count`` into that many identical
            synthetic hits; a scan with a few hundred clusters averaging
            hundreds of photos each could balloon into hundreds of thousands
            of hits, and every one of them got checked against every one of
            the profile's pin boundaries in ``_match_hits_to_pins`` - easily
            slow enough to trip a proxy's read timeout on submit). Summed
            instead of counting list length wherever a suggestion's
            ``hit_count`` is derived.
        extra_dates: Additional distinct ISO dates this hit's ``weight``
            covers, beyond ``taken_at`` itself - lets one representative hit
            still carry a whole cluster's full date spread into
            ``visit_dates`` (see ``_dates_from_hits``).
    """

    latitude: float
    longitude: float
    taken_at: datetime.datetime
    label: str | None = None
    asset_id: str | None = None
    source_key: str | None = None
    weight: int = 1
    extra_dates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """Counts and identifiers returned to the caller for a progress/toast message."""

    matched_suggestions: int
    new_pin_suggestions: int
    hits_processed: int
    #: Maps each hit's ``source_key`` (when present) to the PinSuggestion pk it
    #: resolved to - lets a local-scan caller learn which suggestion a
    #: submitted cluster became, e.g. to upload opt-in candidate photos to it.
    suggestion_ids_by_key: dict[str, int] = field(default_factory=dict)


def _dates_from_hits(hits: list[LocationHit]) -> list[str]:
    """Return the distinct, sorted, capped ISO dates among a list of hits."""
    dates = {hit.taken_at.date().isoformat() for hit in hits}
    dates.update(*(hit.extra_dates for hit in hits))
    return sorted(dates)[:MAX_STORED_VISIT_DATES]


def _weight_of(hits: list[LocationHit]) -> int:
    """Return the total photo-equivalent count a list of hits stands in for."""
    return sum(hit.weight for hit in hits)


def _merge_dates(existing: list[str], new: list[str]) -> list[str]:
    """Return the union of two date-string lists, sorted and capped."""
    return sorted(set(existing) | set(new))[:MAX_STORED_VISIT_DATES]


def _centroid(hits: list[LocationHit]) -> tuple[float, float]:
    """Return the weight-weighted mean (latitude, longitude) of a list of hits.

    Weighting matters once a single hit can stand in for many identically-
    located photos (see ``LocationHit.weight``) - a cluster of 500 photos
    merging with one of 2 should still pull the centroid mostly toward the
    500-photo cluster, exactly as it would if every one of those 500 photos
    were still its own separate hit.
    """
    total_weight = sum(hit.weight for hit in hits)
    return (
        sum(hit.latitude * hit.weight for hit in hits) / total_weight,
        sum(hit.longitude * hit.weight for hit in hits) / total_weight,
    )


def _label_from_hits(hits: list[LocationHit]) -> str:
    """Return the first non-empty hit label, or an empty string."""
    for hit in hits:
        if hit.label:
            return hit.label
    return ""


def _merge_sample_assets(existing: list[dict[str, str]], hits: list[LocationHit]) -> list[dict[str, str]]:
    """Return existing sample assets plus any new ones from hits, deduped and capped.

    Args:
        existing: Current ``PinSuggestion.sample_assets`` value.
        hits: Hits being merged into the suggestion this call.

    Returns:
        Updated list, deduplicated by asset_id and capped at ``MAX_SUGGESTION_PHOTOS``.
    """
    seen = {sample["asset_id"] for sample in existing}
    merged = list(existing)
    for hit in hits:
        if len(merged) >= MAX_SUGGESTION_PHOTOS:
            break
        if hit.asset_id and hit.asset_id not in seen:
            merged.append({"asset_id": hit.asset_id, "taken_at": hit.taken_at.date().isoformat()})
            seen.add(hit.asset_id)
    return merged


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


def _upsert_matched_suggestion(profile: Profile, pin: Pin, hits: list[LocationHit], origin: PinSuggestionOrigin) -> PinSuggestion:
    """Create or extend the pending suggestion to log visit(s) on an existing pin."""
    dates = _dates_from_hits(hits)
    existing = PinSuggestion.objects.filter(profile=profile, pin=pin, status=PinSuggestionStatus.PENDING).first()
    if existing is not None:
        existing.visit_dates = _merge_dates(existing.visit_dates, dates)
        existing.hit_count += _weight_of(hits)
        existing.sample_assets = _merge_sample_assets(existing.sample_assets, hits)
        existing.save(update_fields=["visit_dates", "hit_count", "sample_assets", "updated"])
        return existing
    return PinSuggestion.objects.create(
        profile=profile,
        pin=pin,
        latitude=pin.effective_latitude,
        longitude=pin.effective_longitude,
        origin=origin,
        visit_dates=dates,
        hit_count=_weight_of(hits),
        sample_assets=_merge_sample_assets([], hits),
    )


def _upsert_new_pin_suggestion(profile: Profile, cluster: list[LocationHit], origin: PinSuggestionOrigin) -> PinSuggestion:
    """Create or extend the pending suggestion to create a new pin for a cluster of hits."""
    latitude, longitude = _centroid(cluster)
    dates = _dates_from_hits(cluster)
    existing = _find_nearby_pending_new_pin_suggestion(profile, latitude, longitude)
    if existing is not None:
        existing.visit_dates = _merge_dates(existing.visit_dates, dates)
        existing.hit_count += _weight_of(cluster)
        existing.sample_assets = _merge_sample_assets(existing.sample_assets, cluster)
        if not existing.suggested_name:
            existing.suggested_name = _label_from_hits(cluster)
        existing.save(update_fields=["visit_dates", "hit_count", "sample_assets", "suggested_name", "updated"])
        return existing
    return PinSuggestion.objects.create(
        profile=profile,
        pin=None,
        latitude=latitude,
        longitude=longitude,
        origin=origin,
        visit_dates=dates,
        hit_count=_weight_of(cluster),
        suggested_name=_label_from_hits(cluster),
        sample_assets=_merge_sample_assets([], cluster),
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
        Summary counts for the calling task/view to report - all zero when
        the profile has turned off visit-history tracking, since a
        PinSuggestion is itself a location-history trail.
    """
    if not visit_logging_allowed(profile):
        return IngestSummary(matched_suggestions=0, new_pin_suggestions=0, hits_processed=0)

    hit_list = list(hits)
    matched, unmatched = _match_hits_to_pins(profile, hit_list)
    suggestion_ids_by_key: dict[str, int] = {}
    for pin, pin_hits in matched.items():
        suggestion = _upsert_matched_suggestion(profile, pin, pin_hits, origin)
        for hit in pin_hits:
            if hit.source_key:
                suggestion_ids_by_key[hit.source_key] = suggestion.pk

    clusters = _cluster_hits(unmatched, CLUSTER_RADIUS_M)
    for cluster in clusters:
        suggestion = _upsert_new_pin_suggestion(profile, cluster, origin)
        for hit in cluster:
            if hit.source_key:
                suggestion_ids_by_key[hit.source_key] = suggestion.pk

    return IngestSummary(
        matched_suggestions=len(matched),
        new_pin_suggestions=len(clusters),
        hits_processed=_weight_of(hit_list),
        suggestion_ids_by_key=suggestion_ids_by_key,
    )


def _delete_image_with_file(image: Image) -> None:
    """Delete an Image row and its stored file together.

    Plain ``QuerySet.delete()``/``Model.delete()`` never touches storage, so
    every deletion site in this codebase deletes the file first. Factored out
    here since accept/reject both need it for candidate photo cleanup.
    """
    image.image.delete(save=False)
    image.delete()


@dataclass(frozen=True, slots=True)
class AcceptResult:
    """What accepting a suggestion produced.

    Attributes:
        pin: The reused or newly-created pin.
        visits: Newly-created PinVisit rows - possibly empty if every date was
            already logged, or if the profile has turned off visit-history
            tracking.
        immich_import_visits: Maps a selected Immich asset id to the PinVisit
            pk it should attach to once downloaded. Accepting a suggestion
            never talks to Immich or enqueues Celery itself - the caller is
            responsible for actually importing these (see
            ``tasks.import_immich_photos``).
    """

    pin: Pin
    visits: list[PinVisit]
    immich_import_visits: dict[str, int]


def _resolve_visit(visit_by_date: dict[datetime.date, PinVisit], day: datetime.date | None, fallback: PinVisit | None) -> PinVisit | None:
    """Return the visit for ``day`` if one exists, else ``fallback``."""
    if day is not None and day in visit_by_date:
        return visit_by_date[day]
    return fallback


def _visit_by_date(pin: Pin, suggestion: PinSuggestion, new_visits: list[PinVisit]) -> dict[datetime.date, PinVisit]:
    """Map every date in ``suggestion.visit_dates`` to its PinVisit, new or pre-existing.

    A selected photo taken on a date that was already logged before this
    accept call (see the skip branch in ``accept_pin_suggestion``) must still
    resolve to that real existing visit rather than being dropped.
    """
    days = [datetime.date.fromisoformat(date_str) for date_str in suggestion.visit_dates]
    by_date: dict[datetime.date, PinVisit] = {visit.visited_at.date(): visit for visit in pin.visit_history.filter(visited_at__date__in=days)}
    for visit in new_visits:
        by_date[visit.visited_at.date()] = visit
    return by_date


def accept_pin_suggestion(
    suggestion: PinSuggestion,
    profile: Profile,
    *,
    image_ids: list[int] | None = None,
    asset_ids: list[str] | None = None,
    name: str | None = None,
    label_ids: list[int] | None = None,
) -> AcceptResult:
    """Accept a pending PinSuggestion: reuse/create its pin and log any missing dated visits.

    Mirrors ``services.memories.photos.create_pin_and_log_visit`` for the new-pin
    case (resolve Location, reuse-or-create a minimal Pin, apply the suggested
    name only if the pin has none yet) and ``services.visits.accept_visit_suggestion``
    for the visit-logging case, except one ``PinVisit`` is created per distinct
    date in ``suggestion.visit_dates`` rather than a single one - a batch scan
    commonly finds several separate visits to the same place.

    Any candidate photos the user selected (``image_ids`` - local-scan opt-in
    uploads staged against this suggestion; ``asset_ids`` - Immich assets from
    ``suggestion.sample_assets``) are attached to the pin and to whichever
    visit matches their capture date. Selected local images graduate from
    candidate to real gallery photos; unselected ones are deleted along with
    their stored files, since they have no further purpose once the
    suggestion is resolved.

    Args:
        suggestion: The pending suggestion being accepted.
        profile: The accepting profile (must be suggestion.profile).
        image_ids: Pks of candidate ``Image`` rows (``pin_suggestion=suggestion``,
            ``profile=profile``) the user chose to keep. Any not selected are
            deleted. Ignored ids (wrong owner/suggestion) are silently skipped.
        asset_ids: Immich asset ids (must be a subset of
            ``suggestion.sample_assets``) the user chose to import.
        name: User-chosen name for a brand-new pin (the "Create pin" dialog),
            taking priority over ``suggestion.suggested_name``. Ignored when
            the suggestion matches an existing pin - that pin's name is never
            overwritten by accepting a suggestion.
        label_ids: Label pks to apply to a brand-new pin, filtered to labels
            visible to ``profile``. Also ignored for an existing pin.

    Returns:
        An :class:`AcceptResult` describing the pin, any newly-created visits,
        and any selected Immich assets still to be imported by the caller.
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
        chosen_name = (name or "").strip() or suggestion.suggested_name
        if chosen_name and pin.name is None:
            pin.name = chosen_name
            pin.name_is_user_provided = True
            pin.save(update_fields=["name", "name_is_user_provided", "updated"])
        if label_ids:
            from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
            from urbanlens.dashboard.models.labels.model import Label

            valid_labels = Label.objects.visible_to(profile).filter(id__in=label_ids, kind__in=(KIND_TAG, KIND_CATEGORY, KIND_STATUS))
            pin.labels.add(*valid_labels)
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

    visit_by_date = _visit_by_date(pin, suggestion, visits)
    fallback_visit = visits[0] if visits else (next(iter(visit_by_date.values()), None))

    selected_images = list(Image.objects.filter(pk__in=image_ids or [], profile=profile, pin_suggestion=suggestion))
    for image in selected_images:
        photo_day = image.taken_at.date() if image.taken_at else None
        image.visit = _resolve_visit(visit_by_date, photo_day, fallback_visit)
        image.pin = pin
        image.location = pin.location
        image.pin_suggestion = None
        image.save(update_fields=["visit", "pin", "location", "pin_suggestion", "updated"])

    selected_ids = {image.pk for image in selected_images}
    for stale in Image.objects.filter(pin_suggestion=suggestion).exclude(pk__in=selected_ids):
        _delete_image_with_file(stale)

    sample_by_id = {sample["asset_id"]: sample for sample in suggestion.sample_assets}
    immich_import_visits: dict[str, int] = {}
    for asset_id in asset_ids or []:
        sample = sample_by_id.get(asset_id)
        if sample is None:
            continue
        asset_day = datetime.date.fromisoformat(sample["taken_at"])
        target = _resolve_visit(visit_by_date, asset_day, fallback_visit)
        if target is not None:
            immich_import_visits[asset_id] = target.pk

    suggestion.pin = pin
    suggestion.status = PinSuggestionStatus.ACCEPTED
    suggestion.save(update_fields=["pin", "location", "status", "updated"])
    return AcceptResult(pin=pin, visits=visits, immich_import_visits=immich_import_visits)


def reject_pin_suggestion(suggestion: PinSuggestion) -> None:
    """Reject a pending PinSuggestion, discarding any staged candidate photos.

    Args:
        suggestion: The pending suggestion being rejected.
    """
    for image in Image.objects.filter(pin_suggestion=suggestion):
        _delete_image_with_file(image)
    suggestion.status = PinSuggestionStatus.REJECTED
    suggestion.save(update_fields=["status", "updated"])
