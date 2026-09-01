"""Boundary-provider abstractions for default Location geometry.

The chain resolves *typed* boundaries: a property boundary (parcel/grounds)
and a building boundary (structure footprint) are looked up independently.
Providers that can't distinguish declare themselves as property sources -
ambiguity always resolves to property. There is no static-bbox fallback any
more: when nothing is found, the effective property boundary is the default
circle around the location's coordinates (see ``Boundary.effective_polygon``),
and a missing building boundary simply means "no known building".

A location's generated boundary isn't cached forever - it's refreshed lazily
after ``SiteSettings.boundary_cache_days`` (see ``boundary_generation_stale``),
the same stale-while-revalidate shape every other page-view-triggered refresh
in this codebase already uses (``LocationCache.is_stale``/``get_fresh``):
the previously-generated geometry is served immediately, a background refresh
is single-flight-scheduled, and the next request (or a client-side poll of an
already-open page) picks up the new geometry once it lands. Unlike
``LocationCache``, background enrichment (``BoundaryEnrichmentSource``) never
proactively revisits a stale row - the same "refreshing stale rows stays the
job of the lazy, request-triggered machinery" rule that source documents for
every other cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider
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


#: Provider ``service_key`` → :class:`BoundarySource` value for the providers
#: whose property geometry may serve as an official-boundary voting candidate.
#: Building-footprint providers (Overture, Microsoft, Google) are absent on
#: purpose: they never produce property boundaries, and the vote is over which
#: *property* boundary should officially represent the location.
PROVIDER_BOUNDARY_SOURCES: dict[str, str] = {
    "redata_boundary": "redata",
    "overpass": "overpass",
}


@dataclass(slots=True)
class ResolvedBoundaries:
    """Typed result of one provider-chain run for a coordinate."""

    property_polygon: MultiPolygon | None = None
    building_polygon: MultiPolygon | None = None
    #: Every property polygon any queried provider returned, as
    #: (service_key, polygon) pairs in chain order - including polygons that
    #: lost the ``property_polygon`` slot to an earlier provider. Feeds the
    #: per-source candidate rows boundary voting chooses between; costs no
    #: extra API calls since only providers the chain already queried appear.
    property_candidates: list[tuple[str, MultiPolygon]] = field(default_factory=list)

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
    """Resolve typed default boundaries by trying providers in order.

    Each provider contributes to whichever boundary-type slots it can fill
    (declared via ``BoundaryProvider.boundary_kind`` or a per-feature
    ``get_typed_boundaries`` override); the chain stops once both slots are
    filled or providers are exhausted. ``RedataBoundaryProvider`` runs first:
    when it has data at all, it's authoritative survey-grade county GIS
    geometry, not community-tagged or ML-derived - but its coverage is
    narrower (US-only, varies by jurisdiction), so every other provider still
    matters as a fallback. It's also a no-op, not an error, for installs that
    haven't configured REData at all (see its own docstring). A Regrid-backed
    provider (parcel/property data) was investigated but never added - it's a
    paid service, and ``RedataBoundaryProvider`` already fills the
    property-boundary slot Regrid would have.
    """

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
            ResolvedBoundaries; either polygon may be None when no provider
            found that boundary type.
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
            except SoftTimeLimitExceeded:
                # The task is being asked to wind down (Celery soft time limit) -
                # this is not a per-provider failure, so it must not be swallowed
                # like one: continuing to the next provider would just burn the
                # remaining time budget and risk the hard time limit SIGKILLing
                # the worker mid-write. Let it propagate so the task exits cleanly.
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


def generation_lock_key(location_id: int) -> str:
    """Cache key for the single-flight lock guarding one Location's generation run.

    Shared between :func:`schedule_location_boundary_generation` (which
    claims it) and ``tasks.generate_boundaries_for_location`` (which releases
    it in a ``finally``), so the two can never drift out of sync on the exact
    key string.
    """
    return f"ul_boundary_generation_{location_id}"


def generation_status(location: Location) -> tuple[bool, bool]:
    """Return (ran, stale) for a Location's place resolution.

    ``boundary_generation_ran`` and ``boundary_generation_stale`` answer
    related questions off the same state, so every caller that needs both
    (``schedule_location_boundary_generation``, the refresh gate in
    ``tasks.generate_boundaries_for_location``) goes through here instead of
    calling both public functions and reading it twice.

    ``place_resolved_at`` - not the place itself - is what marks that the
    chain ran, so a coordinate the providers genuinely know nothing about is
    asked about once and then left alone until it goes stale, rather than
    re-queried on every page view.
    """
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

    Says nothing about freshness - a location whose generation is years old
    still returns True here. See :func:`boundary_generation_stale` for that.

    Args:
        location: The Location to check.

    Returns:
        True when the location-default property row exists with ``generated_at`` set.
    """
    return generation_status(location)[0]


def boundary_generation_stale(location: Location) -> bool:
    """True when a Location's generated boundary is older than the site's cache window.

    A never-generated location is not "stale" - it's "pending" (see
    :func:`boundary_generation_ran`); callers check that first. This only
    answers the separate question of whether an *existing* generation is due
    for a background refresh.

    Args:
        location: The Location to check.

    Returns:
        True when the location-default property row's ``generated_at`` is
        older than ``SiteSettings.boundary_cache_days``. False when never
        generated, or still fresh.
    """
    return generation_status(location)[1]


def schedule_location_boundary_generation(location: Location, profile=None) -> bool:
    """Ensure default-boundary generation is in flight for a Location, single-flight.

    Covers both a never-generated location (nothing to show yet - callers
    surface this as "pending") and a stale one due for a background refresh
    (callers already have a - possibly stale - boundary to show, and should
    surface this as "refreshing" instead, never as "pending"). Used by pages
    that aren't pin-scoped (the wiki page); Private Pin pages go through the
    "boundary" panel source for the never-generated case, and this function
    directly for the stale-refresh case, since the panel source's own
    single-flight/readiness plumbing has no stale-but-serve concept.

    Args:
        location: The Location to generate boundaries for.
        profile: The requesting user's profile; generation is skipped when the
            profile has external APIs disabled.

    Returns:
        True when generation is in flight (newly scheduled or already
        running), False when it's already fresh, not allowed, or the Celery
        broker was unreachable.
    """
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


def generate_location_boundaries(location: Location, *, name: str | None = None) -> Place | None:
    """Resolve a Location onto a real-world place, provisioning geometry if needed.

    The single choke point every boundary-generation call site funnels through
    (wiki creation, the Private Pin page's boundary panel, the wiki page's own
    scheduler). It answers "what is this coordinate standing on?" rather than
    "what shape should I draw here?", which is the change that stops one
    property accumulating a copy of its own outline per person who pinned it.

    Cheap by default: if a known place already contains the coordinate, no
    provider is called at all. That is the common case on any property more
    than one person has pinned, and the reason importing 124 buildings onto a
    campus now costs one parcel lookup rather than 124.

    A run that finds nothing leaves any previously-generated geometry alone
    rather than erasing good data over a transient provider hiccup, and stamps
    ``Location.place_resolved_at`` regardless so the chain is not re-run on
    every page view (until it goes stale - see ``boundary_generation_stale``).

    The chain's heavy steps (downloading and gunzipping building-footprint
    shards, shapely geometry work) mean this belongs in a Celery worker, never
    on the request path.

    Args:
        location: The Location to place.
        name: Optional place name hint; defaults to the location's official name.

    Returns:
        The resolved place, or None when no provider knows this coordinate.
    """
    from urbanlens.dashboard.services.places.provisioning import ensure_place_for_location

    place = ensure_place_for_location(location, name=name)
    if location.place_resolved_at is None:
        # Nothing resolved and nothing provisioned: still record that we asked,
        # so an unknown coordinate is queried once rather than on every view.
        from urbanlens.dashboard.services.places.resolution import attach_location

        attach_location(location, None)

    # A place's outline can only newly exist or change right here, so this is
    # also the right point to re-derive what the markers standing on it are,
    # and to check whether this location's wiki (if any) now nests under - or
    # now contains - another one. Both are no-ops for the overwhelming
    # majority of locations, which have no wiki and no siblings.
    from urbanlens.dashboard.services.locations.site_scope import reclassify_markers_on_place
    from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting_for_location

    if place is not None:
        reclassify_markers_on_place(place)
        if place.parcel is not None and place.parcel.pk != place.pk:
            reclassify_markers_on_place(place.parcel)
    reconcile_wiki_nesting_for_location(location)

    return place
