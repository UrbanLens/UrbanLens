"""QuerySet and Manager for LocationExposure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.measure import D

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin_share.exposure import ExposureSource, LocationExposure


class LocationExposureQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for LocationExposure models."""

    def near(self, profile_id: int, location: Location, *, radius_meters: int) -> LocationExposureQuerySet:
        """Exposure rows for ``profile_id`` within ``radius_meters`` of ``location``.

        The one spatial query every resolution/propagation step in
        ``services.share_provenance`` shares, so "same place" always means the
        same thing (a radius match, never an exact Location-row match) no
        matter which caller is asking.

        Args:
            profile_id: PK of the profile whose exposures to search.
            location: The place to match against.
            radius_meters: Match radius, in meters.

        Returns:
            A queryset of matching exposure rows.
        """
        return self.filter(
            profile_id=profile_id,
            location__point__distance_lte=(location.point, D(m=radius_meters)),
        )


class LocationExposureManager(abstract.DashboardManager.from_queryset(LocationExposureQuerySet)):
    """Custom query manager for LocationExposure models."""

    def record(self, *, profile_id: int, location_id: int, share_id: int, source: ExposureSource) -> tuple[LocationExposure, bool]:
        """Get-or-create the (profile, location, share) exposure row.

        Consolidates the near-identical ``get_or_create`` calls scattered
        across ``services.share_provenance`` (``record_share_exposure``,
        ``propagate_exposures_for_pin_move``), which only ever differed in
        ``source``. Any ``DatabaseError`` is left to the caller - this
        performs no exception handling of its own, matching plain
        ``get_or_create`` behavior.

        Args:
            profile_id: PK of the exposed profile.
            location_id: PK of the exposed location.
            share_id: PK of the share that delivered the exposure.
            source: Why this exposure exists (see ``ExposureSource``).

        Returns:
            ``(exposure, created)``, exactly like ``get_or_create``.
        """
        return self.get_or_create(
            profile_id=profile_id,
            location_id=location_id,
            share_id=share_id,
            defaults={"source": source},
        )
