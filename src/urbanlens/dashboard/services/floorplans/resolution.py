"""
Which floorplan a building shows: the user's own first, an upstream provider's as backfill.
When only a provider answers, its document is returned read-only (its ``origin`` key) rather than silently copied into rows, so a future better upstream version is never shadowed by a stale mirror."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.points import FLOORPLAN_PROVIDERS

if TYPE_CHECKING:
    import datetime as dt

    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class FloorplanProvider(ABC):
    """An upstream source of floorplans for a building, contributed by a plugin to ``points.FLOORPLAN_PROVIDERS``."""

    #: The ``origin`` the documents it returns carry.
    origin: ClassVar[str]
    #: Said in the editor of a document of this origin, which is read-only until saved as the viewer's own.
    origin_banner: ClassVar[str]

    def available(self) -> bool:
        """Whether the provider can be asked on this install."""
        return True

    @abstractmethod
    def document(self, place: Place, on_date: dt.date | None) -> dict[str, Any] | None:
        """
        The provider's plan for the building, or None for any form of absence.

        A floorplan is never load-bearing for a page, so upstream trouble reads as no plan.

        Args:
            place: The building.
            on_date: Resolve the plan as of this date; None for current.
        """

    def building_ref(self, place: Place) -> str:
        """
        The ref a plan saved for ``place`` carries so the provider's later plans find it, or blank.

        Args:
            place: The building.
        """
        return ""


def provider_building_ref(place: Place | None) -> str:
    """The first non-blank ref a provider gives a plan saved for ``place``."""
    if place is None:
        return ""
    return next((ref for provider in FLOORPLAN_PROVIDERS if (ref := provider.building_ref(place))), "")


def resolve_document(place: Place, *, profile: Profile | None = None, on_date: dt.date | None = None) -> dict[str, Any] | None:
    """
    The floorplan document for a building, resolved by date.
    So local lookup is profile-scoped, and a provider's aggregated plans - external, non-personal by construction - are what a user without their own plan sees.

    Args:
        place: The building.
        profile: Whose plans to consider local.
        on_date: Resolve the plan as of this date; None for current.

    Returns:
        The document with an ``origin`` key (``"local"``, ``"community"`` or a provider's) and, for a provider's, an ``origin_banner``, or None when no side has a plan - the overwhelmingly common case, answered by one indexed query and, at most, one upstream call."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.floorplans.serialization import document_for

    local = Floorplan.objects.at(place, on_date, profile=profile) if profile is not None else None
    if local is not None:
        return {**document_for(local), "origin": "local"}

    community = _community_plan(place, profile, on_date)
    if community is not None:
        return {**document_for(community), "origin": "community"}

    return _provider_document(place, on_date)


def resolve_floorplan_row(place: Place, *, profile: Profile | None = None, on_date: dt.date | None = None) -> Floorplan | None:
    """
    The floorplan *row* (not a serialized document) a building shows, resolved by date.
    Same local-then-community fallback as :func:`resolve_document`, for a caller that needs the actual row to query its geometry - the GeoJSON features endpoint - rather than the pre-serialized document dict.

    Args:
        place: The building.
        profile: Whose plans to consider local, and whose wiki access to check for the community fallback.
        on_date: Resolve the plan as of this date; None for current.

    Returns:
        The matching row, or None when neither side has one."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan

    if profile is None:
        return None
    local = Floorplan.objects.at(place, on_date, profile=profile)
    if local is not None:
        return local
    return _community_plan(place, profile, on_date)


def _community_plan(place: Place, profile: Profile | None, on_date: dt.date | None):
    """
    The published plan for this building, when the user can see its wiki.
    Community plans follow the wiki's own visibility rule rather than inventing a second one: a user sees the wiki (and so its floorplan) once they have discovered the place - see ``services.wiki.wiki_access``."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.services.wiki.wiki_access import place_visible_to

    if profile is None or not place_visible_to(place, profile):
        return None
    return Floorplan.objects.at(place, on_date, community=True)


def _provider_document(place: Place, on_date: dt.date | None) -> dict[str, Any] | None:
    """The first provider's plan for this building, or None for any form of absence."""
    for provider in FLOORPLAN_PROVIDERS:
        if not provider.available():
            continue
        try:
            document = provider.document(place, on_date)
        except Exception:
            logger.debug("floorplans: %s lookup failed for %s", provider.origin, place.provider_key, exc_info=True)
            continue
        if document is not None:
            return {**document, "origin": provider.origin, "origin_banner": provider.origin_banner}
    return None


def publish_to_wiki(floorplan: Floorplan, profile: Profile) -> Floorplan | None:
    """
    Copy a personal plan onto the place's community wiki.
    A copy rather than a hand-over: the author keeps their own version, and the community one is a separate row anyone who can see the wiki may edit from then on.

    Args:
        floorplan: The personal version to publish.
        profile: The publishing author, credited on the wiki edit.

    Returns:
        The community plan, or None when the place has no wiki to publish to."""
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
    """
    Whether this profile may edit a published plan.

    Args:
        floorplan: A community (wiki-owned) plan.
        profile: The would-be editor.

    Returns:
        True when the plan is community-owned and its wiki is visible here."""
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
    on_date: dt.date | None = None,
    allow_community: bool = False,
):
    """
    The local floorplan version a save should write into.
    Writing is deliberately narrow, because a floorplan is expensive hand work and a save must never destroy someone else's:

    Args:
        place: The building, or None for a plan not tied to one.
        profile: The editing user.
        pin: The pin the plan belongs to.
        version_uuid: The plan version the document came from, if any.
        on_date: ``valid_from`` for a newly created version.
        allow_community: Whether the caller may write a wiki-owned row in place.

    Returns:
        A saved local ``Floorplan`` row the caller may write into.

    Raises:
        ValueError: If neither a place nor a pin is given, which would leave the plan unreachable."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan

    if place is None and pin is None:
        raise ValueError("A floorplan needs either a place or a pin to belong to.")

    scope = {"place": place} if place is not None else {"place__isnull": True, "pin": pin}

    if version_uuid:
        named = Floorplan.objects.filter(uuid=version_uuid, **scope).first()
        if named is not None and named.wiki_id is None and named.profile_id == profile.pk:
            return named
        # A published plan is community content: anyone who can see the wiki edits the same row, the
        # way every other wiki field works - but only when the caller says it meant to.
        # A save arriving here with a community plan's uuid and no such intent replaces the author's
        # work with whatever happened to be on the saver's screen.
        if named is not None and allow_community and can_edit_community(named, profile):
            return named
    return Floorplan.objects.create(
        place=place,
        pin=pin,
        profile=profile,
        valid_from=on_date,
        building_ref=provider_building_ref(place),
    )
