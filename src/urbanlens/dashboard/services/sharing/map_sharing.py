"""Central hook for "one profile's MarkupMap became visible to another"."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.services.sharing.map_pin_share_detection import sync_pin_inferences

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

#: How close (metres) and how recent (days) an unresolved map-detected share
#: must be to a freshly-created pin for `infer_source_share_for_pin` to link
#: them. Approximate by nature - see module docstring on that function.
INFERRED_SOURCE_SHARE_RADIUS_METERS = 150
INFERRED_SOURCE_SHARE_WINDOW_DAYS = 30


def _record_detected_share(sender: Profile, recipient: Profile, pin: Pin, markup_map: MarkupMap) -> PinShare | None:
    """Create a MAP_DETECTED PinShare for ``(pin, recipient)`` if one doesn't already exist.

    Args:
        sender: The profile whose pin was revealed (owns ``pin``).
        recipient: The profile the map was sent to.
        pin: The pin detected as shared.
        markup_map: The map whose detection produced this match.

    Returns:
        The newly created PinShare, or None if one already existed for this (pin, recipient) pair (an earlier explicit share, or an earlier send/detection pass)."""
    from urbanlens.dashboard.services.sharing.share_provenance import record_share_exposure, resolve_and_stamp_origin_share

    if PinShare.objects.already_shared_with(recipient, pin=pin).exists():
        return None
    share = PinShare.objects.create(
        pin=pin,
        location=pin.location,
        from_profile=sender,
        to_profile=recipient,
        # Same reshare-chain rule as the explicit share flows: the share this
        # pin was accepted from, a prior exposure at its location, or the
        # best-effort map heuristic (see services.sharing.share_provenance).
        parent_share=resolve_and_stamp_origin_share(pin),
        origin=PinShareOrigin.MAP_DETECTED,
        status=PinShareStatus.DETECTED,
        detected_via_map=markup_map,
    )
    record_share_exposure(share)
    return share


def share_markup_map_with_profile(sender: Profile, recipient: Profile, markup_map: MarkupMap) -> list[PinShare]:
    """Run pin-share detection for a map being sent from ``sender`` to ``recipient``.

    Args:
        sender: ``markup_map``'s owner at the time of sending.
        recipient: The profile the map is being shared with.
        markup_map: The map being shared.

    Returns:
        Newly created PinShare rows (empty if nothing was detected, or everything was already recorded from a prior send of this or another map covering the same pins)."""
    pins = sync_pin_inferences(markup_map)
    shares = []
    for pin in pins:
        share = _record_detected_share(sender, recipient, pin, markup_map)
        if share is not None:
            shares.append(share)
    return shares


def clone_markup_map(source: MarkupMap, recipient: Profile, sender: Profile) -> MarkupMap:
    """"Add to my maps": clone ``source`` (owned by ``sender``) into ``recipient``'s own maps.
    Reuses ``MarkupMap.to_snapshot()``/``replace_items_from_snapshot()`` - the same round-trip already used by ``materialize_markup_map`` - so item cloning logic isn't duplicated.

    Args:
        source: The map being cloned (may or may not still be owned by ``sender`` - ``shared_by`` records who sent it regardless).
        recipient: The profile the clone will belong to.
        sender: The profile who most recently sent ``source`` to ``recipient`` (shown as "From X" on the clone).

    Returns:
        The newly created clone, owned by ``recipient``."""
    new_map = MarkupMap.objects.create(profile=recipient, title=source.title, cloned_from=source, shared_by=sender)
    new_map.replace_items_from_snapshot(source.to_snapshot())
    return new_map


def infer_source_share_for_pin(pin: Pin) -> PinShare | None:
    """Best-effort match of a self-created pin to a prior inbound map-detected share.

    Args:
        pin: The pin to find a plausible inbound share for.

    Returns:
        The best-matching PinShare, or None if no plausible match exists."""
    from datetime import timedelta

    from django.contrib.gis.geos import Point
    from django.utils import timezone

    if not pin.location_id:
        return None

    cutoff = timezone.now() - timedelta(days=INFERRED_SOURCE_SHARE_WINDOW_DAYS)
    candidates = PinShare.objects.map_detected_candidates(pin.profile_id, since=cutoff)

    target = Point(float(pin.location.longitude), float(pin.location.latitude), srid=4326)
    best: PinShare | None = None
    best_distance: float | None = None
    for candidate in candidates:
        candidate_location = candidate.shared_location
        if candidate_location is None:
            continue
        candidate_point = Point(float(candidate_location.longitude), float(candidate_location.latitude), srid=4326)
        distance_meters = target.distance(candidate_point) * 111_320.0
        if distance_meters > INFERRED_SOURCE_SHARE_RADIUS_METERS:
            continue
        if best_distance is None or distance_meters < best_distance:
            best, best_distance = candidate, distance_meters
    return best
