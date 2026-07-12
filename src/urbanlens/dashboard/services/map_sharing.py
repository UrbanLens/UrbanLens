"""Central hook for "one profile's MarkupMap became visible to another".

Every place a MarkupMap gets handed to a different profile - a DM attachment,
a standalone map share, or a map attached to an explicit pin share - should
call :func:`share_markup_map_with_profile` exactly once for that send, so the
geometry-based pin-share detection in ``services.map_pin_share_detection``
lives in one place instead of being reimplemented per send-path. This module
also holds the "Add to my maps" clone helper (:func:`clone_markup_map`).

Detected pin shares are recorded as ordinary
:class:`~urbanlens.dashboard.models.pin_share.model.PinShare` rows (with
``origin=PinShareOrigin.MAP_DETECTED`` and ``status=PinShareStatus.DETECTED``)
so the existing sharing stats (``PinShare.chain_share_count``, the Memories >
Sharing page) pick them up transparently - see the model docstrings for why
that status is never actionable and never materializes a Pin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.services.map_pin_share_detection import detect_shared_pins

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

    Skips creation when *any* share (explicit or previously detected) already
    exists for this exact (pin, recipient) pair - not just a prior detected
    one - so a map attachment that happens to also reveal the very pin being
    explicitly shared in the same action doesn't double-count that share in
    the stats (``PinShare.chain_share_count`` counts rows, not unique pins).

    Args:
        sender: The profile whose pin was revealed (owns ``pin``).
        recipient: The profile the map was sent to.
        pin: The pin detected as shared.
        markup_map: The map whose detection produced this match.

    Returns:
        The newly created PinShare, or None if one already existed for this
        (pin, recipient) pair (an earlier explicit share, or an earlier
        send/detection pass).
    """
    if PinShare.objects.filter(pin=pin, to_profile=recipient).exists():
        return None
    return PinShare.objects.create(
        pin=pin,
        from_profile=sender,
        to_profile=recipient,
        # Reuses the same reshare-chain rule as the explicit share flows
        # (controllers.pin_sharing, services.pin_sharing): prefer the share
        # this pin was accepted from, falling back to the best-effort
        # heuristic link for pins the owner created themselves.
        parent_share_id=pin.source_share_id or pin.inferred_source_share_id,
        origin=PinShareOrigin.MAP_DETECTED,
        status=PinShareStatus.DETECTED,
        detected_via_map=markup_map,
    )


def share_markup_map_with_profile(sender: Profile, recipient: Profile, markup_map: MarkupMap) -> list[PinShare]:
    """Run pin-share detection for a map being sent from ``sender`` to ``recipient``.

    This is the single entrypoint every send-path (DM attach-send, the
    standalone map-share action, the pin-share dialog's optional map
    attachment) should call once the underlying send has been decided. It
    never creates or sends the DM/notification/share itself - only records
    any pins the map reveals - and never clones or otherwise materializes
    anything on the recipient's account (see :func:`clone_markup_map` for
    that, a separate and only user-initiated action).

    Args:
        sender: ``markup_map``'s owner at the time of sending.
        recipient: The profile the map is being shared with.
        markup_map: The map being shared.

    Returns:
        Newly created PinShare rows (empty if nothing was detected, or
        everything was already recorded from a prior send of this or another
        map covering the same pins).
    """
    pins = detect_shared_pins(markup_map, sender)
    shares = []
    for pin in pins:
        share = _record_detected_share(sender, recipient, pin, markup_map)
        if share is not None:
            shares.append(share)
    return shares


def clone_markup_map(source: MarkupMap, recipient: Profile, sender: Profile) -> MarkupMap:
    """"Add to my maps": clone ``source`` (owned by ``sender``) into ``recipient``'s own maps.

    Reuses ``MarkupMap.to_snapshot()``/``replace_items_from_snapshot()`` - the
    same round-trip already used by ``materialize_markup_map`` - so item
    cloning logic isn't duplicated.

    Args:
        source: The map being cloned (may or may not still be owned by
            ``sender`` - ``shared_by`` records who sent it regardless).
        recipient: The profile the clone will belong to.
        sender: The profile who most recently sent ``source`` to ``recipient``
            (shown as "From X" on the clone).

    Returns:
        The newly created clone, owned by ``recipient``.
    """
    new_map = MarkupMap.objects.create(profile=recipient, title=source.title, cloned_from=source, shared_by=sender)
    new_map.replace_items_from_snapshot(source.to_snapshot())
    return new_map


def infer_source_share_for_pin(pin: Pin) -> PinShare | None:
    """Best-effort match of a self-created pin to a prior inbound map-detected share.

    When a profile creates their own Pin (not by accepting a PinShare) near a
    location that was previously revealed to them via a shared map, this pin
    has no ``source_share`` to anchor a reshare chain to if they later share
    it explicitly. This heuristically links it to the most plausible prior
    detection: the nearest unresolved ``MAP_DETECTED`` share to this profile
    within :data:`INFERRED_SOURCE_SHARE_RADIUS_METERS` metres and
    :data:`INFERRED_SOURCE_SHARE_WINDOW_DAYS` days.

    This is inherently approximate (proximity + recency, no user
    confirmation) - it is only ever used as a fallback when ``source_share``
    itself is unset, and only at the moment the pin is explicitly shared
    onward (see ``controllers.pin_sharing.PinShareCreateView``), never at pin
    creation time.

    Args:
        pin: The pin to find a plausible inbound share for. Must have a
            ``location``.

    Returns:
        The best-matching PinShare, or None if no plausible match exists.
    """
    from datetime import timedelta

    from django.contrib.gis.geos import Point
    from django.utils import timezone

    if not pin.location_id:
        return None

    cutoff = timezone.now() - timedelta(days=INFERRED_SOURCE_SHARE_WINDOW_DAYS)
    candidates = PinShare.objects.filter(
        to_profile=pin.profile_id,
        origin=PinShareOrigin.MAP_DETECTED,
        created__gte=cutoff,
    ).select_related("pin__location")

    target = Point(float(pin.location.longitude), float(pin.location.latitude), srid=4326)
    best: PinShare | None = None
    best_distance: float | None = None
    for candidate in candidates:
        candidate_location = candidate.pin.location
        if candidate_location is None:
            continue
        candidate_point = Point(float(candidate_location.longitude), float(candidate_location.latitude), srid=4326)
        distance_meters = target.distance(candidate_point) * 111_320.0
        if distance_meters > INFERRED_SOURCE_SHARE_RADIUS_METERS:
            continue
        if best_distance is None or distance_meters < best_distance:
            best, best_distance = candidate, distance_meters
    return best
