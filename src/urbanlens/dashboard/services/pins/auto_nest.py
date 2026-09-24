"""Turning a property's buildings into child pins and child wikis without being asked.

A sweep can run any number of times - on pin creation, when the building list lands or is refreshed, when the
property's boundary arrives - and converges: each building gets one child pin standing on its own child wiki,
a building that appears later is added, and one whose pin the owner deleted or moved is left alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.building_clusters import BuildingCluster, SweptBuilding

logger = logging.getLogger(__name__)

#: How long a panel read waits before it may ask for another sweep of the same pin.
SWEEP_REQUEST_THROTTLE_SECONDS = 600

#: Distinct buildings a property needs before its buildings get pins of their own. One: a lone building is
#: still a building, and its pin is where its own name and records go rather than onto the property's.
MIN_BUILDINGS_TO_NEST = 1


def eligible(pin: Pin) -> bool:
    """Whether a pin may have building children created for it unprompted.

    Args:
        pin: The candidate parent.

    Returns:
        True for a root pin whose owner has the feature on, that was never told "no" via the restructure offer,
        is not user-typed as a single structure, and - unless an earlier sweep built it - has no children yet
        (a hierarchy the owner arranged by hand is theirs)."""
    from urbanlens.dashboard.models.pin.model import PinType

    if pin.parent_pin_id is not None or pin.restructure_offer_dismissed:
        return False
    if not pin.profile.auto_create_building_pins:
        return False
    if pin.pin_type_is_user_provided and pin.pin_type in (PinType.BUILDING, PinType.ENTRANCE):
        return False
    return pin.buildings_auto_nested_at is not None or not pin.detail_pins.exists()


def swept_buildings(pin: Pin) -> list[SweptBuilding]:
    """Where earlier sweeps placed this pin's building pins."""
    from urbanlens.dashboard.services.pins.building_clusters import SweptBuilding

    return [swept for value in pin.auto_nested_buildings or [] if (swept := SweptBuilding.from_json(value)) is not None]


def _remembered(swept: list[SweptBuilding], clusters: list[BuildingCluster], pinned: set[int]) -> list[dict]:
    """``swept`` plus every building that now has a pin, one entry per point."""
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate
    from urbanlens.dashboard.services.pins.building_clusters import SweptBuilding
    from urbanlens.dashboard.services.pins.pin_restructure import MAX_RESTRUCTURE_ITEMS

    entries = [*swept, *(SweptBuilding(clusters[index].latitude, clusters[index].longitude, ref=min(clusters[index].refs, default="")) for index in sorted(pinned))]
    unique: dict[tuple, SweptBuilding] = {}
    for entry in entries:
        unique.setdefault((quantize_coordinate(entry.effective_latitude, "latitude"), quantize_coordinate(entry.effective_longitude, "longitude")), entry)
    return [entry.to_json() for entry in list(unique.values())[-MAX_RESTRUCTURE_ITEMS * 2 :]]


def auto_nest_pin(pin: Pin) -> int:
    """Give every building on this pin's property a child pin and a child wiki.

    A no-op unless the property's real boundary is known and at least one building is known on it; a property
    with none stays unswept so it can nest once its buildings become known.

    Args:
        pin: The parent pin.

    Returns:
        How many child pins were created."""
    from urbanlens.dashboard.models.pin.model import Pin as PinModel
    from urbanlens.dashboard.services.pins.building_clusters import distinct_building_count, match_clusters
    from urbanlens.dashboard.services.pins.pin_restructure import BuildingNester, WikiMirror, building_markers

    if not eligible(pin):
        return 0

    with transaction.atomic():
        # Serialises sweeps of one pin: the panel fetch, the boundary refresh and pin creation can all ask at once.
        locked = PinModel.objects.select_for_update(of=("self",)).select_related("location", "profile").filter(pk=pin.pk).first()
        if locked is None or not eligible(locked):
            return 0
        nester = BuildingNester.for_pin(locked)
        if nester.boundary is None or distinct_building_count(nester.clusters) < MIN_BUILDINGS_TO_NEST:
            return 0

        everything = set(range(len(nester.clusters)))
        mirror = WikiMirror()
        # The same gate the pin's own save applies before it creates a wiki (signals.ensure_wiki_for_pin_location).
        if locked.profile.community_enabled:
            try:
                with transaction.atomic():
                    mirror = nester.mirror_wikis(everything, None)
            except Exception:
                # Losing the wikis must not lose the pins; the next sweep retries them.
                logger.exception("auto_nest: wiki mirror failed for pin %s", locked.pk)

        swept = swept_buildings(locked)
        created = nester.create_pins(everything, mirror.wikis, swept=swept)
        matched, _unmatched = match_clusters(nester.clusters, building_markers(locked.descendants().select_related("location")))
        PinModel.objects.filter(pk=locked.pk).update(buildings_auto_nested_at=timezone.now(), auto_nested_buildings=_remembered(swept, nester.clusters, set(matched)))

    pin.refresh_from_db(fields=["buildings_auto_nested_at", "auto_nested_buildings"])
    if created:
        logger.info("auto_nest: created %d building pin(s) under pin %s", len(created), pin.pk)
        _refresh_property_names(locked)
        from urbanlens.dashboard.services.pins.external_data import seed_site_descendants
        from urbanlens.dashboard.services.pins.source_documents import warm_site_scope_documents

        seed_site_descendants(locked)

        # A document fetch that ran before the sweep cached this pin's single-building answer.
        warm_site_scope_documents(locked)
    return len(created)


def auto_nest_location(location: Location) -> int:
    """Sweep every eligible root pin standing on a location.

    Args:
        location: The location whose building list or boundary just arrived.

    Returns:
        How many child pins were created across all pins."""
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    created = 0
    for pin in PinModel.objects.filter(location=location, parent_pin__isnull=True).select_related("location", "profile"):
        try:
            created += auto_nest_pin(pin)
        except Exception:
            # One user's failure (a constraint collision, say) must not block
            # another's sweep, nor the fetch that triggered it.
            logger.exception("auto_nest: sweep failed for pin %s", pin.pk)
    return created


def request_location_sweep(location: Location) -> None:
    """Queue a sweep of every root pin on a location once the current transaction commits.

    Args:
        location: The location whose boundary or buildings just changed.
    """
    from urbanlens.dashboard.models.pin.model import Pin as PinModel

    pin_ids = list(PinModel.objects.filter(location=location, parent_pin__isnull=True).values_list("pk", flat=True))
    for pin_id in pin_ids:
        _enqueue_sweep(pin_id)


def request_sweep(pin: Pin) -> None:
    """Ask for a background sweep of one pin, at most once per throttle window.

    Args:
        pin: A root pin a reader just found buildings without child pins on.
    """
    from django.core.cache import cache

    if eligible(pin) and cache.add(f"auto-nest-sweep:{pin.pk}", 1, timeout=SWEEP_REQUEST_THROTTLE_SECONDS):
        _enqueue_sweep(pin.pk)


def _enqueue_sweep(pin_id: int) -> None:
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import auto_nest_building_pins

    transaction.on_commit(lambda: safely_enqueue_task(auto_nest_building_pins, pin_id))


def _refresh_property_names(pin: Pin) -> None:
    """Re-judge a property's names and aliases once its buildings have places: a campus stops carrying one building's name.

    Args:
        pin: The swept parent pin.
    """
    from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

    if pin.location is None:
        return
    try:
        update_location_name_from_external_sources(pin.location)
    except Exception:
        logger.exception("auto_nest: name refresh failed for location %s", pin.location_id)
