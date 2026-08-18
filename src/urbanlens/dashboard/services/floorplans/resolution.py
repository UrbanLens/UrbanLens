"""Which floorplan a building shows: the user's own first, REData's as backfill.

Local plans are authored by people standing in the building; REData's (none
exist yet - the endpoint is speculative) would be aggregated from whatever
provider it eventually finds. When both exist, local wins. When only REData
answers, its document is returned read-only (``"origin": "redata"``) rather
than silently copied into rows, so a future better upstream version is never
shadowed by a stale mirror.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def resolve_document(place: Place, *, on_date: datetime.date | None = None) -> dict[str, Any] | None:
    """The floorplan document for a building, resolved by date.

    Args:
        place: The building.
        on_date: Resolve the plan as of this date; None for current.

    Returns:
        The document with an ``origin`` key (``"local"`` or ``"redata"``), or
        None when neither side has a plan - the overwhelmingly common case,
        answered by one indexed query and, at most, one upstream call.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.floorplans.serialization import document_for

    local = Floorplan.objects.at(place, on_date)
    if local is not None:
        return {**document_for(local), "origin": "local"}

    return _redata_document(place, on_date)


def _redata_document(place: Place, on_date: datetime.date | None) -> dict[str, Any] | None:
    """REData's plan for this building, or None for any form of absence.

    Two steps, matching REData's API: the parcel-scoped summary list names the
    version in force, then the detail endpoint returns its document. The
    parcel uuid comes from the building place's parent parcel when that was
    provisioned from REData; without one there is nothing to ask.
    """
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
    from urbanlens.UrbanLens.settings.app import settings

    if place.provider != "redata" or not place.provider_key:
        return None
    if not settings.redata_api_url or not settings.redata_api_key:
        return None
    parcel = place.parent if place.parent_id else None
    if parcel is None or parcel.provider != "redata" or not parcel.provider_key:
        return None
    try:
        gateway = RedataGateway()
        summaries = gateway.lookup_floorplans(parcel.provider_key, building_ref=place.provider_key, on_date=on_date.isoformat() if on_date else None)
        if not summaries:
            return None
        document = gateway.lookup_floorplan_document(str(summaries[0].get("uuid") or ""))
    except Exception:
        # A floorplan is never load-bearing for a page; upstream trouble reads
        # as "no plan" and the local editor still works.
        logger.debug("floorplans: REData lookup failed for %s", place.provider_key, exc_info=True)
        return None
    if document is None:
        return None
    return {**document, "origin": "redata"}


def floorplan_for_editing(place: Place, profile: Profile, *, on_date: datetime.date | None = None):
    """The local floorplan version to edit, creating an empty one if needed.

    Editing never touches a REData-origin document - the user's plan starts
    empty (or from their existing local version) and simply wins resolution
    from then on.

    Args:
        place: The building.
        profile: The editing user.
        on_date: The version date being edited; None edits the current one.

    Returns:
        A saved local ``Floorplan`` row.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan

    existing = Floorplan.objects.at(place, on_date)
    if existing is not None:
        return existing
    return Floorplan.objects.create(place=place, profile=profile, valid_from=on_date, building_ref=place.provider_key if place.provider == "redata" else "")
