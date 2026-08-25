"""Which floorplan a building shows: the user's own first, REData's as backfill.

Local plans are authored by people standing in the building; REData's (none
exist yet - the endpoint is speculative) would be aggregated from whatever
provider it eventually finds. When both exist, local wins. When only REData
answers, its document is returned read-only (``"origin": "redata"``) rather
than silently copied into rows, so a future better upstream version is never
shadowed by a stale mirror.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def resolve_document(place: Place, *, profile: Profile | None = None, on_date: datetime.date | None = None) -> dict[str, Any] | None:
    """The floorplan document for a building, resolved by date.

    A local plan is the author's own: it records where the doors are, what
    locks them and what opens those locks, which is not something to hand to
    everyone who pins the same building. So local lookup is profile-scoped,
    and REData's aggregated plans - external, non-personal by construction -
    are what a user without their own plan sees.

    Args:
        place: The building.
        profile: Whose plans to consider local. None looks at no local plans
            at all (a caller with no user in hand gets the upstream answer).
        on_date: Resolve the plan as of this date; None for current.

    Returns:
        The document with an ``origin`` key (``"local"`` or ``"redata"``), or
        None when neither side has a plan - the overwhelmingly common case,
        answered by one indexed query and, at most, one upstream call.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.floorplans.serialization import document_for

    local = Floorplan.objects.at(place, on_date, profile=profile) if profile is not None else None
    if local is not None:
        return {**document_for(local), "origin": "local"}

    community = _community_plan(place, profile, on_date)
    if community is not None:
        return {**document_for(community), "origin": "community"}

    return _redata_document(place, on_date)


def _community_plan(place: Place, profile: Profile | None, on_date: datetime.date | None):
    """The published plan for this building, when the user can see its wiki.

    Community plans follow the wiki's own visibility rule rather than
    inventing a second one: a user sees the wiki (and so its floorplan) once
    they have discovered the place - see ``services.wiki.wiki_access``.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.wiki.wiki_access import place_visible_to

    if profile is None or not place_visible_to(place, profile):
        return None
    return Floorplan.objects.at(place, on_date, community=True)


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


def publish_to_wiki(floorplan: Floorplan, profile: Profile) -> Floorplan | None:
    """Copy a personal plan onto the place's community wiki.

    A copy rather than a hand-over: the author keeps their own version, and
    the community one is a separate row anyone who can see the wiki may edit
    from then on. Publishing is explicit for the reason the read scope is
    narrow - a plan names doors, locks and what opens them.

    Args:
        floorplan: The personal version to publish.
        profile: The publishing author, credited on the wiki edit.

    Returns:
        The community plan, or None when the place has no wiki to publish to.
    """
    from django.core.exceptions import ObjectDoesNotExist
    from django.db import transaction

    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.wiki_edit import WikiEdit
    from urbanlens.dashboard.services.floorplans.serialization import document_for, save_document

    # A plan not tied to a place has no community to publish to: the wiki
    # being published onto is the *place's*, and "somewhere I sketched from
    # memory" has no such thing.
    if floorplan.place is None:
        return None
    try:
        wiki = floorplan.place.wiki
    except ObjectDoesNotExist:
        return None

    document = document_for(floorplan)
    with transaction.atomic():
        existing = Floorplan.objects.filter(place=floorplan.place, wiki=wiki, valid_from=floorplan.valid_from).first()
        community = existing or Floorplan.objects.create(
            place=floorplan.place,
            wiki=wiki,
            profile=profile,
            valid_from=floorplan.valid_from,
            building_ref=floorplan.building_ref,
        )
        # The copy's items are new rows, so the personal plan's own items are
        # never moved out from under it.
        document.pop("uuid", None)
        save_document(community, document, profile=profile)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"floorplan": {"from": None if existing is None else "updated", "to": community.name or "published"}},
        )
    return community


def can_edit_community(floorplan: Floorplan, profile: Profile) -> bool:
    """Whether this profile may edit a published plan.

    The wiki's own rule, not a second one: anyone who can see the wiki can
    edit its content.

    Args:
        floorplan: A community (wiki-owned) plan.
        profile: The would-be editor.

    Returns:
        True when the plan is community-owned and its wiki is visible here.
    """
    from urbanlens.dashboard.services.wiki.wiki_access import place_visible_to

    if floorplan.place is None:
        return False
    return floorplan.wiki_id is not None and place_visible_to(floorplan.place, profile)


def floorplan_for_editing(
    place: Place | None,
    profile: Profile,
    *,
    pin=None,
    version_uuid: str = "",
    on_date: datetime.date | None = None,
    allow_community: bool = False,
):
    """The local floorplan version a save should write into.

    Writing is deliberately narrow, because a floorplan is expensive hand
    work and a save must never destroy someone else's:

    - A save naming a version *this profile owns* updates it in place.
    - A save naming a *published* version updates it too, when the wiki is
      visible here and the caller passed *allow_community* - community
      content is edited in place by design, but only deliberately.
    - Anything else - no uuid, an unknown uuid, a REData-origin document, or
      another user's personal version - creates a new version owned by the
      saver. The document's item uuids simply won't match the new
      version's (empty) contents, so its contents are recreated rather than
      moved.

    That also makes dating explicit: changing ``valid_from`` on a loaded plan
    edits that plan's date, while "save as a new version" (the editor drops
    the plan uuid) creates a second version and leaves the original standing.

    A plan need not belong to a known building. When *place* is None the plan
    is scoped to *pin* instead - a placeless plan is one person's drawing of
    one pin, so the pin is its identity. Scoping by ``place=None`` would match
    every placeless plan on the site at once, which is why *pin* is required
    in that case.

    Args:
        place: The building, or None for a plan not tied to one.
        profile: The editing user.
        pin: The pin the plan belongs to. Required when *place* is None.
        version_uuid: The plan version the document came from, if any.
        on_date: ``valid_from`` for a newly created version.
        allow_community: Whether the caller may write a wiki-owned row in
            place. Off by default: a whole-document save deletes by omission,
            and the ordinary save path is reached by a debounced autosave that
            nobody explicitly asked for. Without it a community plan forks
            into the saver's own version instead of being overwritten.

    Returns:
        A saved local ``Floorplan`` row the caller may write into.

    Raises:
        ValueError: If neither a place nor a pin is given, which would leave
            the plan unreachable.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan

    if place is None and pin is None:
        raise ValueError("A floorplan needs either a place or a pin to belong to.")

    scope = {"place": place} if place is not None else {"place__isnull": True, "pin": pin}

    if version_uuid:
        named = Floorplan.objects.filter(uuid=version_uuid, **scope).first()
        if named is not None and named.wiki_id is None and named.profile_id == profile.pk:
            return named
        # A published plan is community content: anyone who can see the wiki
        # edits the same row, the way every other wiki field works - but only
        # when the caller says it meant to. A save arriving here with a
        # community plan's uuid and no such intent replaces the author's work
        # with whatever happened to be on the saver's screen.
        if named is not None and allow_community and can_edit_community(named, profile):
            return named
    return Floorplan.objects.create(
        place=place,
        pin=pin,
        profile=profile,
        valid_from=on_date,
        building_ref=place.provider_key if place is not None and place.provider == "redata" else "",
    )
