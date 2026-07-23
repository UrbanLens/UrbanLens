"""Share-chain provenance: who first told whom about a place, gaming-proof.

The reshare chain (``PinShare.parent_share`` / ``PinShare.chain_share_count``)
must credit the original sharer even when the recipient tries to launder the
place through mutable state:

- receive a share, pin it, move the pin away, drop a *new* pin at the original
  spot and share that one (pin-keyed tracking breaks);
- receive a share, pin it, move the pin to a new location and share it from
  there (location-keyed tracking breaks);
- any number of pin delete / re-create cycles in between.

The fix is to track *both*: every share received creates a
:class:`~urbanlens.dashboard.models.pin_share.exposure.LocationExposure` for
``(recipient, shared location)``, and moving a pin propagates its owner's
exposures (plus the pin's own share lineage) onto the new location. Exposures
never reference the recipient's pins, so deleting/re-creating pins cannot
clear them, and any future pin within :data:`EXPOSURE_RADIUS_METERS` of an
exposed location chains its onward shares back to the originating share.

Two independent mechanisms cooperate, and it matters which one is in play:

- A pin that itself carries lineage (``source_share`` from accepting a share,
  or ``inferred_source_share`` stamped by an earlier
  :func:`resolve_and_stamp_origin_share` call) resolves through *that* field
  directly, regardless of where the pin currently sits - moving it any
  distance, even repeatedly, never loses the chain.
- A pin with no lineage of its own resolves *live*, by radius, against its
  *current* location every time it's about to be shared. Every step that
  touches this radius match - the live resolution, the duplicate-share dedup
  check, and propagation across a move - must use the same radius query
  (``LocationExposure.objects.near``), or a pin that was only ever *near*
  (not exactly on) an exposed spot can silently lose the trail across a
  second move.

Every code path that creates a ``PinShare`` - the share dialog, ``@pin`` chat
shares, map-geometry detection, DM coordinate/address detection, trip
activities - should set ``parent_share`` via :func:`resolve_origin_share` and
then call :func:`record_share_exposure` on the new row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.measure import D
from django.db import DatabaseError

from urbanlens.dashboard.models.pin_share.exposure import ExposureSource, LocationExposure

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_share.model import PinShare

logger = logging.getLogger(__name__)

#: How close (metres) a location must be to an exposed location to count as
#: "the same place" for chain resolution. Matches the map-detection
#: inference radius (services.map_sharing.INFERRED_SOURCE_SHARE_RADIUS_METERS)
#: but, unlike it, applies with no time window - exposure is forever.
EXPOSURE_RADIUS_METERS = 150


def find_profile_pin_near_location(profile_id: int, location: Location | None, *, radius_meters: int = EXPOSURE_RADIUS_METERS) -> Pin | None:
    """The profile's top-level pin at (or within ``radius_meters`` of) ``location``.

    Args:
        profile_id: PK of the pin owner to search.
        location: The place to match against; None short-circuits to None.
        radius_meters: Match radius; defaults to :data:`EXPOSURE_RADIUS_METERS`.

    Returns:
        The nearest-created matching Pin, or None. An exact same-Location pin
        is preferred over a proximity match.
    """
    if location is None:
        return None
    from urbanlens.dashboard.models.pin.model import Pin

    exact = Pin.objects.filter(profile_id=profile_id, parent_pin__isnull=True, location_id=location.pk).first()
    if exact is not None:
        return exact
    return Pin.objects.filter(
        profile_id=profile_id,
        parent_pin__isnull=True,
        location__point__distance_lte=(location.point, D(m=radius_meters)),
    ).first()


def record_share_exposure(share: PinShare, *, source: ExposureSource = ExposureSource.SHARE_RECEIVED) -> LocationExposure | None:
    """Record that ``share.to_profile`` learned about the shared place via ``share``.

    Skipped when the recipient already has their own pin at the place - the
    share then wasn't their *initial* information, so their future shares of
    it must not chain under this one. (Pins created *after* the exposure and
    later deleted don't un-record it - exposures are permanent.)

    Args:
        share: The just-created share to record.
        source: Why this exposure exists (see ``ExposureSource``).

    Returns:
        The (created or pre-existing) exposure row, or None when the share has
        no location or the recipient already knew the place first-hand.
    """
    location = share.shared_location
    if location is None:
        return None
    if find_profile_pin_near_location(share.to_profile_id, location) is not None:
        return None
    try:
        exposure, _created = LocationExposure.objects.record(
            profile_id=share.to_profile_id,
            location_id=location.pk,
            share_id=share.pk,
            source=source,
        )
    except DatabaseError:
        logger.exception("Could not record exposure for share %s", share.pk)
        return None
    return exposure


def profile_is_exposed_to(profile_id: int, location: Location, *, radius_meters: int = EXPOSURE_RADIUS_METERS) -> bool:
    """Whether ``profile_id`` already has any exposure within ``radius_meters`` of ``location``.

    Used by the detection paths (DM coordinates, trip activities) to avoid
    piling duplicate detected-share rows onto a place the recipient was
    already told about - the chain only needs the first one.

    Args:
        profile_id: PK of the profile to check.
        location: The place being revealed.
        radius_meters: Match radius; defaults to :data:`EXPOSURE_RADIUS_METERS`.

    Returns:
        True when an exposure already covers this place for this profile.
    """
    return LocationExposure.objects.near(profile_id, location, radius_meters=radius_meters).exists()


def resolve_origin_share(profile_id: int, *, pin: Pin | None = None, location: Location | None = None) -> PinShare | None:
    """The share through which ``profile_id`` originally learned about a place.

    This is the single parent-share rule for every share-creation path.
    Resolution order:

    1. ``pin.source_share`` - the pin was created by accepting a share.
    2. ``pin.inferred_source_share`` - a previously persisted heuristic link.
    3. The profile's earliest ``LocationExposure`` within
       :data:`EXPOSURE_RADIUS_METERS` of the place - survives pin moves,
       deletes, and re-creates.
    4. The legacy map-detection heuristic
       (``services.map_sharing.infer_source_share_for_pin``), as a last
       resort for pre-exposure data.

    Args:
        profile_id: The prospective sharer (whose provenance is in question).
        pin: The pin being shared, when one exists.
        location: The place being shared; defaults to ``pin.location``.

    Returns:
        The originating PinShare to use as ``parent_share``, or None when the
        profile discovered the place independently.
    """
    if pin is not None:
        if pin.source_share_id is not None:
            return pin.source_share
        if pin.inferred_source_share_id is not None:
            return pin.inferred_source_share

    if location is None and pin is not None:
        location = pin.location
    if location is not None:
        exposure = LocationExposure.objects.near(profile_id, location, radius_meters=EXPOSURE_RADIUS_METERS).select_related("share").order_by("created").first()
        if exposure is not None:
            return exposure.share

    if pin is not None and pin.location_id is not None:
        from urbanlens.dashboard.services.map_sharing import infer_source_share_for_pin

        return infer_source_share_for_pin(pin)
    return None


def resolve_and_stamp_origin_share(pin: Pin) -> PinShare | None:
    """Resolve a pin's origin share and persist it on the pin when it was heuristic.

    Same as :func:`resolve_origin_share`, but when the pin carried no lineage
    of its own (``source_share`` / ``inferred_source_share`` both unset) and a
    parent was found via exposures or the map heuristic, the result is stored
    as ``inferred_source_share`` - so the pin itself now carries the lineage
    for future resolutions and move propagation.

    Args:
        pin: The pin about to be shared onward.

    Returns:
        The originating PinShare, or None.
    """
    parent = resolve_origin_share(pin.profile_id, pin=pin)
    if parent is not None and pin.source_share_id is None and pin.inferred_source_share_id is None:
        pin.inferred_source_share = parent
        try:
            pin.save(update_fields=["inferred_source_share", "updated"])
        except DatabaseError:
            logger.exception("Could not stamp inferred source share on pin %s", pin.pk)
    return parent


def propagate_exposures_for_pin_move(pin: Pin, old_location_id: int | None) -> int:
    """Carry a moved pin's infection from its old location onto the new one.

    Called from ``Pin.save`` whenever a persisted pin's ``location`` changes.
    Two propagation rules:

    - Every exposure the owner had *near* the old location (a radius match,
      not an exact Location-row match - the pin may have been sitting close
      to, but not exactly on, an exposed spot, e.g. a distinct nearby
      Location row from ``get_nearby_or_create``'s dedup threshold) is copied
      to the new one, so sharing the moved pin (or any future pin dropped at
      the new spot) still chains back to the original share. This also
      covers a *second* move of a pin that was never itself shared/stamped
      while sitting in the old radius - without the radius match here, that
      exposure would be silently missed and the chain would break.
    - When the pin itself carries share lineage (``source_share`` /
      ``inferred_source_share``), an exposure for that share is ensured at
      both the old and new locations - the pin "touched" both places.

    Args:
        pin: The pin that just moved (already saved with its new location).
        old_location_id: The Location pk the pin moved away from.

    Returns:
        The number of exposure rows created.
    """
    if pin.location_id is None or old_location_id is None or pin.location_id == old_location_id:
        return 0

    from urbanlens.dashboard.models.location.model import Location

    created = 0
    lineage_share_id = pin.source_share_id or pin.inferred_source_share_id
    try:
        old_location = Location.objects.filter(pk=old_location_id).only("pk", "point").first()
        share_ids: set[int] = set()
        if old_location is not None:
            share_ids.update(LocationExposure.objects.near(pin.profile_id, old_location, radius_meters=EXPOSURE_RADIUS_METERS).values_list("share_id", flat=True))
        if lineage_share_id is not None:
            share_ids.add(lineage_share_id)
        for share_id in share_ids:
            _exposure, was_created = LocationExposure.objects.record(
                profile_id=pin.profile_id,
                location_id=pin.location_id,
                share_id=share_id,
                source=ExposureSource.PIN_MOVED,
            )
            created += int(was_created)
        # The pin also touched (and may have been shared from) the old spot;
        # make sure its own lineage is recorded there for future pins at it.
        if lineage_share_id is not None:
            _exposure, was_created = LocationExposure.objects.record(
                profile_id=pin.profile_id,
                location_id=old_location_id,
                share_id=lineage_share_id,
                source=ExposureSource.PIN_MOVED,
            )
            created += int(was_created)
    except DatabaseError:
        logger.exception("Could not propagate exposures for pin %s move %s -> %s", pin.pk, old_location_id, pin.location_id)
    return created
