"""QuerySet and Manager for LocationExposure and PinShare."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.gis.measure import D

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable
    import datetime

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_share.exposure import ExposureSource, LocationExposure
    from urbanlens.dashboard.models.profile.model import Profile


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


class PinShareQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for PinShare models."""

    def already_shared_with(self, recipient: Profile | int, *, pin: Pin | None = None, location: Location | None = None) -> PinShareQuerySet:
        """Whether ``pin`` (or, when there's no pin, ``location``) has already been shared with ``recipient``.

        Two independent dedup checks that share one purpose - avoid sending
        the same place to the same recipient twice - keyed differently
        depending on whether the sharer has their own pin at the place yet.
        Exactly one of ``pin``/``location`` should be given.

        Args:
            recipient: The prospective recipient profile (or a raw pk).
            pin: The sharer's pin, when they have one.
            location: The place being shared, when there's no pin yet.

        Returns:
            Matching share rows - callers typically just check ``.exists()``.
        """
        if pin is not None:
            return self.filter(pin=pin, to_profile=recipient)
        return self.filter(location=location, to_profile=recipient)

    def reusable_for(self, recipient: Profile | int, location: Location) -> PinShareQuerySet:
        """Earlier still-actionable shares of ``location`` already sent to ``recipient``.

        Used when a place was already shared before: rather than creating a
        duplicate, later mentions (e.g. a repeated DM detection) reuse the
        earliest pending/detected share so the recipient's "Add to map"
        affordance points at one consistent share.

        Args:
            recipient: The recipient profile (or a raw pk).
            location: The shared place.

        Returns:
            Matching shares, oldest first.
        """
        from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

        return self.filter(to_profile=recipient, location=location, status__in=[PinShareStatus.PENDING, PinShareStatus.DETECTED]).order_by("created")

    def pending_pin_ids_for(self, recipient: Profile | int, pins: Iterable[Pin]) -> QuerySet[Any, Any]:
        """PKs of ``pins`` that already have a pending share to ``recipient``.

        Used when bundling a pin's descendants into one share so an already-
        pending child share isn't duplicated.

        Args:
            recipient: The recipient profile (or a raw pk).
            pins: Candidate pins (e.g. ``pin.descendants()``).

        Returns:
            A flat ``values_list`` queryset of pin pks.
        """
        from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

        return self.filter(to_profile=recipient, status=PinShareStatus.PENDING, pin__in=pins).values_list("pin_id", flat=True)

    def sent_by(self, profile: Profile | int) -> PinShareQuerySet:
        """Shares sent by ``profile`` (the outgoing half of the Sharing page).

        Args:
            profile: The sender profile (or a raw pk).

        Returns:
            Matching shares, unordered (callers apply their own ordering).
        """
        return self.filter(from_profile=profile)

    def received_by(self, profile: Profile | int) -> PinShareQuerySet:
        """Shares received by ``profile`` (the incoming half of the Sharing page).

        Args:
            profile: The recipient profile (or a raw pk).

        Returns:
            Matching shares, unordered (callers apply their own ordering).
        """
        return self.filter(to_profile=profile)

    def map_detected_candidates(self, profile_id: int, *, since: datetime.datetime):
        """Recent map-detected shares to ``profile_id``, for source-share inference.

        Args:
            profile_id: PK of the recipient whose shares to search.
            since: Only shares created at or after this time qualify.

        Returns:
            Matching shares with ``pin__location`` pre-selected.
        """
        from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin

        return self.filter(to_profile=profile_id, origin=PinShareOrigin.MAP_DETECTED, created__gte=since).select_related("pin__location")


class PinShareManager(abstract.DashboardManager.from_queryset(PinShareQuerySet)):
    """Custom query manager for PinShare models."""
