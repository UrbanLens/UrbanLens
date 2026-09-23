"""Boundary-provider abstractions for default Location geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, BoundaryProviderDeferredError
from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.microsoft_buildings import MicrosoftBuildingFootprintsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place

logger = logging.getLogger(__name__)


def _as_multipolygon(geom: Polygon | MultiPolygon | None) -> MultiPolygon | None:
    """Normalize a polygonal geometry to MultiPolygon (SRID preserved)."""
    if geom is None:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon(geom, srid=geom.srid)
    return geom


#: Provider ``service_key`` → :class:`BoundarySource` value for the providers whose property geometry
#: may serve as an official-boundary voting candidate.
#: Building-footprint providers (Overture, Microsoft, Google) are absent on purpose: they never
#: produce property boundaries, and the vote is over which *property* boundary should officially
PROVIDER_BOUNDARY_SOURCES: dict[str, str] = {
    "redata_boundary": "redata",
    "overpass": "overpass",
}


@dataclass(slots=True)
class ResolvedBoundaries:
    """Typed result of one provider-chain run for a coordinate."""

    property_polygon: MultiPolygon | None = None
    building_polygon: MultiPolygon | None = None
    #: Every property polygon any queried provider returned, as (service_key, polygon) pairs in chain
    #: order - including polygons that lost the ``property_polygon`` slot to an earlier provider.
    #: Feeds the per-source candidate rows boundary voting chooses between; costs no extra API calls
    property_candidates: list[tuple[str, MultiPolygon]] = field(default_factory=list)
    #: Providers that declined for now (throttled, source budget spent). Their silence is not a "nothing here".
    deferred: list[str] = field(default_factory=list)
    #: The longest wait any deferring provider asked for, in seconds.
    retry_after: int | None = None

    def polygon_for(self, boundary_type: str) -> MultiPolygon | None:
        """The resolved polygon for a :class:`BoundaryType` value, or None."""
        if boundary_type == "building":
            return self.building_polygon
        return self.property_polygon

    @property
    def complete(self) -> bool:
        """True when both boundary types have been resolved."""
        return self.property_polygon is not None and self.building_polygon is not None


@dataclass(slots=True)
class BoundaryProviderChain:
    """Resolve typed default boundaries by trying providers in order."""

    providers: tuple[BoundaryProvider, ...] = field(
        default_factory=lambda: (
            RedataBoundaryProvider(),
            OverpassGateway(),
            OvertureMapsGateway(),
            MicrosoftBuildingFootprintsGateway(),
            GoogleOpenBuildingsGateway(),
        ),
    )

    def get_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> ResolvedBoundaries:
        """Run the chain and return typed boundaries for a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Optional place name forwarded to name-aware providers.

        Returns:
            ResolvedBoundaries; either polygon may be None when no provider found that boundary type.
        """
        resolved = ResolvedBoundaries()
        for provider in self.providers:
            if resolved.complete:
                break
            # A single-kind provider whose slot is already filled has nothing to add;
            # providers overriding get_typed_boundaries may fill either slot.
            single_kind = type(provider).get_typed_boundaries is BoundaryProvider.get_typed_boundaries
            if single_kind and resolved.polygon_for(provider.boundary_kind) is not None:
                continue
            try:
                typed = provider.get_typed_boundaries(latitude, longitude, name=name)
            except BoundaryProviderDeferredError as exc:
                resolved.deferred.append(exc.service_key)
                if exc.retry_after is not None:
                    resolved.retry_after = max(resolved.retry_after or 0, exc.retry_after)
                continue
            except SoftTimeLimitExceeded:
                # The task is being asked to wind down (Celery soft time limit) - this is not a
                # per-provider failure, so it must not be swallowed like one: continuing to the next
                # provider would just burn the remaining time budget and risk the hard time limit
                # SIGKILLing the worker mid-write.
                raise
            except Exception:
                # TODO: Catch specific exception
                logger.exception("Boundary provider %s failed for %s,%s", provider.service_key, redact_coordinate(latitude), redact_coordinate(longitude))
                continue
            property_polygon = _as_multipolygon(typed.get("property"))
            if property_polygon is not None and provider.service_key:
                resolved.property_candidates.append((provider.service_key, property_polygon))
            if resolved.property_polygon is None:
                resolved.property_polygon = property_polygon
            if resolved.building_polygon is None:
                resolved.building_polygon = _as_multipolygon(typed.get("building"))
        return resolved

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | MultiPolygon | None:
        """Untyped convenience lookup: the property boundary, else the building one.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Optional place name forwarded to name-aware providers.

        Returns:
            The best available polygon, or None when nothing was found.
        """
        resolved = self.get_boundaries(latitude, longitude, name=name)
        return resolved.property_polygon or resolved.building_polygon


#: Scheduled retries of a place resolution that a provider deferred. Past this a page visit still retries,
#: since a deferred miss is never recorded as a miss.
MAX_DEFERRED_RETRIES = 4

#: First retry delay for a deferred resolution when the provider named no wait; doubles per attempt.
DEFERRED_RETRY_BASE_SECONDS = 900


def generation_lock_key(location_id: int) -> str:
    """Cache key for the single-flight lock guarding one Location's generation run.
    Shared between :func:`schedule_location_boundary_generation` (which claims it) and ``tasks.generate_boundaries_for_location`` (which releases it in a ``finally``), so the two can never drift out of sync on the exact key string."""
    return f"ul_boundary_generation_{location_id}"


def generation_status(location: Location) -> tuple[bool, bool]:
    """Return (ran, stale) for a Location's place resolution."""
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if location.place_resolved_at is None:
        return False, False
    max_age_days = SiteSettings.get_current().boundary_cache_days
    generated_at = location.place.geometry_generated_at if (location.place_id and location.place is not None) else None
    reference = generated_at or location.place_resolved_at
    stale = timezone.now() - reference > timedelta(days=max_age_days)
    return True, stale


def boundary_generation_ran(location: Location) -> bool:
    """True when the provider chain has already run for a Location at least once.

    Args:
        location: The Location to check.

    Returns:
        True when the location-default property row exists with ``generated_at`` set."""
    return generation_status(location)[0]


def boundary_generation_stale(location: Location) -> bool:
    """True when a Location's generated boundary is older than the site's cache window.
    This only answers the separate question of whether an *existing* generation is due for a background refresh.

    Args:
        location: The Location to check.

    Returns:
        True when the location-default property row's ``generated_at`` is older than ``SiteSettings.boundary_cache_days``."""
    return generation_status(location)[1]


def schedule_location_boundary_generation(location: Location, profile=None) -> bool:
    """Ensure default-boundary generation is in flight for a Location, single-flight.

    Args:
        location: The Location to generate boundaries for.
        profile: The requesting user's profile; generation is skipped when the profile has external APIs disabled.

    Returns:
        True when generation is in flight (newly scheduled or already running), False when it's already fresh, not allowed, or the Celery broker was unreachable."""
    from django.core.cache import cache

    if location.latitude is None or location.longitude is None:
        return False
    if profile is not None and not profile.external_apis_enabled:
        return False
    ran, stale = generation_status(location)
    if ran and not stale:
        return False
    lock_key = generation_lock_key(location.pk)
    if cache.add(lock_key, 1, 600):
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import generate_boundaries_for_location

        if safely_enqueue_task(generate_boundaries_for_location, location.pk) is None:
            # Broker down: release the lock we just claimed so the next poll
            # retries the enqueue instead of waiting out the 600s lock behind
            # a task that was never actually queued (mirrors schedule_panel_fetch).
            cache.delete(lock_key)
            return False
    return True


def generate_location_boundaries(location: Location, *, name: str | None = None, force: bool = False, attempt: int = 0) -> Place | None:
    """Resolve a Location onto a real-world place, provisioning geometry if needed.
    It answers "what is this coordinate standing on?" rather than "what shape should I draw here?", which is the change that stops one property accumulating a copy of its own outline per person who pinned it.

    Args:
        location: The Location to place.
        name: Optional place name hint; defaults to the location's official name.
        force: Re-run the provider chain even when the coordinate already resolves onto a fresh place.
        attempt: How many deferred retries preceded this run.

    Returns:
        The resolved place, or None when no provider knows this coordinate."""
    from urbanlens.dashboard.services.places.provisioning import ensure_place_outcome

    outcome = ensure_place_outcome(location, name=name, force=force)
    place = outcome.place
    if outcome.deferred:
        _schedule_deferred_retry(location, outcome.retry_after, attempt=attempt, force=place is not None)
    if location.place_resolved_at is None and not outcome.deferred:
        # Nothing resolved and nothing provisioned: still record that we asked,
        # so an unknown coordinate is queried once rather than on every view.
        # A deferred miss is not recorded: the provider that knows the answer did not give one.
        from urbanlens.dashboard.services.places.resolution import attach_location

        attach_location(location, None)

    # A place's outline can only newly exist or change right here, so this is also the right point
    # to re-derive what the markers standing on it are, and to check whether this location's wiki
    # (if any) now nests under - or now contains - another one.
    # Both are no-ops for the overwhelming majority of locations, which have no wiki and no
    from urbanlens.dashboard.services.locations.site_scope import reclassify_markers_on_place
    from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting_for_location

    if place is not None:
        reclassify_markers_on_place(place)
        if place.parcel is not None and place.parcel.pk != place.pk:
            reclassify_markers_on_place(place.parcel)
        # The outline is what lets a campus pin's buildings nest, so its arrival is a reason to sweep them.
        from urbanlens.dashboard.services.pins.auto_nest import request_location_sweep

        request_location_sweep(location)
        _retry_wikipedia_miss(location)
    reconcile_wiki_nesting_for_location(location)

    return place


def _schedule_deferred_retry(location: Location, retry_after: int | None, *, attempt: int, force: bool) -> None:
    """Ask the provider chain again once the deferring provider's wait is over.

    Args:
        location: The Location whose resolution was deferred.
        retry_after: The provider's requested wait, in seconds, if it named one.
        attempt: How many retries preceded this run.
        force: Re-run the chain even though a fallback provider already placed the location, so the
            authoritative outline can replace the fallback's.
    """
    if attempt >= MAX_DEFERRED_RETRIES:
        logger.info("Place resolution for location %s still deferred after %d retries", location.pk, attempt)
        return
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import generate_boundaries_for_location

    countdown = max(retry_after or 0, DEFERRED_RETRY_BASE_SECONDS * 2**attempt)
    safely_enqueue_task(generate_boundaries_for_location, location.pk, countdown=countdown, force=force, attempt=attempt + 1)


def _retry_wikipedia_miss(location: Location) -> None:
    """Ask Wikipedia again once a location has a parcel, if the last answer was a miss.

    An article Wikipedia places on the parcel matches it (``plugins.builtin.wikipedia.match_outline``), so a
    miss cached before the parcel was known may no longer be one.

    Args:
        location: The location that now stands on a place.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    rows = LocationCache.objects.filter(location=location, source="wikipedia")
    if not rows.exists() or rows.filter(data__has_key="title").exclude(data__title="").exists():
        return
    LocationCache.objects.filter(location=location, source__in=("wikipedia", "wikipedia_media")).delete()

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import prefetch_location_external_data

    transaction.on_commit(lambda: safely_enqueue_task(prefetch_location_external_data, location.pk))
