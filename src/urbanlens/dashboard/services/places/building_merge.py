"""
Merging duplicate building places - one real building recorded as two ``Place`` rows - into one (REData P98).

REData keyed Overture buildings on a content hash that changed whenever a release reshaped or renamed a building, so
one building could be provisioned twice: once under an old hash, once under the next (or under its stable
``overture:<gers_id>`` once REData served it). ``rekey_overture_places`` moves a legacy place onto its stable ref, but
refuses when another place already holds it - that is a duplicate, and this module merges it.

Detection, and what is merged:

- **Same stable ref - merged.** A current legacy-keyed place REData resolves (``/buildings/resolve/``), or still serves
  by that exact ref, as ``overture:<gers_id>``, grouped with the place holding that key and any other legacy place that
  resolves to it. The link is REData's own record of the building's identity, not geometry.
- **Reported, never merged** (each needs a person):
  ``different_parcels`` - a stable-ref group whose members stand on different parcels;
  ``stable_ref_not_current`` - the key is held by a superseded place;
  ``aggregate_member`` - a member is, or belongs to, an earned aggregate;
  ``footprint_only`` - a legacy place matched to a stable ref only because its outline and a REData footprint hold
  each other's centroids;
  ``cross_source_footprint`` - two current building places from different sources (Overture, OSM, CRIS, ...) whose
  outlines hold each other's centroids;
  ``placeless_wiki`` - a building wiki with no place whose location stands inside a building place with its own wiki.

The merge, per loser, is lossless and idempotent: its locations (and with them every pin, audience and wiki access)
move to the survivor; its wiki moves, or merges into the survivor's (``services.wiki.duplicate_wikis``); per-key rows
keep the survivor's value and record the loser's in a :class:`PlaceMerge`; the loser itself stays, SUPERSEDED with its
provider key cleared. A profile left with a pin on each side gets a ``PinMergeSuggestion`` - pins are never merged here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from urbanlens.dashboard.models.place.merge import PlaceMerge
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation, PlaceStatus
from urbanlens.dashboard.services.core.row_merge import snapshot_row
from urbanlens.dashboard.services.places import lineage, overture_refs

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

# How a legacy place is tied to a stable ref.
_BY_REF = "ref"
_BY_FOOTPRINT = "footprint"
_NO_LINK = "none"
_UNAVAILABLE = "unavailable"

# Finding kinds - reported, never merged.
DIFFERENT_PARCELS = "different_parcels"
STABLE_REF_NOT_CURRENT = "stable_ref_not_current"
AGGREGATE_MEMBER = "aggregate_member"
FOOTPRINT_ONLY = "footprint_only"
CROSS_SOURCE_FOOTPRINT = "cross_source_footprint"
PLACELESS_WIKI = "placeless_wiki"

_SUGGESTION_REASON = "Both pins are on the same building, which had been recorded twice."


@dataclass(slots=True)
class DuplicateGroup:
    """
    Places REData's identity record says are one building, with the one that stays.

    Attributes:
        stable_ref: The building's ``overture:<gers_id>``.
        survivor: The place that stays: the one holding ``stable_ref``, else the oldest with a wiki, else the oldest.
        losers: The places merged into it.
    """

    stable_ref: str
    survivor: Place
    losers: list[Place]

    @property
    def gers_id(self) -> str:
        """The Overture GERS id the group shares."""
        return self.stable_ref.removeprefix(overture_refs.OVERTURE_PREFIX)

    @property
    def members(self) -> list[Place]:
        """Survivor first, then the losers."""
        return [self.survivor, *self.losers]


@dataclass(frozen=True, slots=True)
class Finding:
    """
    A possible duplicate that is reported and left alone.

    Attributes:
        kind: One of the finding kinds above.
        places: The places involved.
        detail: Why it was not merged.
        wikis: The wikis involved, for ``placeless_wiki``.
    """

    kind: str
    places: tuple[int, ...]
    detail: str
    wikis: tuple[int, ...] = ()


@dataclass(slots=True)
class Detection:
    """Everything :func:`detect_duplicate_buildings` found."""

    groups: list[DuplicateGroup] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Legacy places REData could not answer for this run - neither merged nor reported; run again.
    unavailable: list[int] = field(default_factory=list)


def _link(
    place: Place,
    *,
    resolver: overture_refs.RedataResolver,
    fetch_buildings: overture_refs.BuildingsFetcher | None,
    locate_parcel: overture_refs.ParcelLocator | None,
    buildings_by_parcel: dict[str, list[dict[str, Any]] | None],
) -> tuple[str, str | None]:
    """
    How one legacy-keyed place is tied to a stable ref: by REData's identity record, by footprint, or not at all.
    Mirrors ``overture_refs._classify`` but keeps apart the two ways it can land on a ref, because only one of them is
    proof enough to merge.

    Returns:
        ``(how, stable_ref)``.
    """
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

    answer = resolver.answer(place.provider_key)
    if answer is None:
        return _UNAVAILABLE, None
    if answer.get("status") == "resolved" and (stable := resolver(place.provider_key)) and not overture_refs.is_legacy_overture_ref(stable):
        return _BY_REF, stable
    if answer.get("status") == "ambiguous" or fetch_buildings is None:
        return _NO_LINK, None
    try:
        parcel_key = overture_refs._redata_parcel_key(place, locate_parcel)  # noqa: SLF001
    except PropertyRecordsUnavailableError:
        return _UNAVAILABLE, None
    if not parcel_key:
        return _NO_LINK, None
    if parcel_key not in buildings_by_parcel:
        try:
            buildings_by_parcel[parcel_key] = fetch_buildings(parcel_key)
        except PropertyRecordsUnavailableError:
            buildings_by_parcel[parcel_key] = None
    buildings = buildings_by_parcel[parcel_key]
    if buildings is None:
        return _UNAVAILABLE, None
    for building in buildings:
        key = overture_refs.building_key(building)
        # REData still serving this exact hash, with its stable ref beside it, is the identity record too.
        if str(building.get("ref") or "").strip() == place.provider_key and key.startswith(overture_refs.OVERTURE_PREFIX) and not overture_refs.is_legacy_overture_ref(key):
            return _BY_REF, key
    outcome, matches = overture_refs._footprint_match(place, buildings)  # noqa: SLF001
    if outcome == overture_refs.FOOTPRINT:
        return _BY_FOOTPRINT, matches[0]
    return _NO_LINK, None


def _parcel_id(place: Place) -> int | None:
    parcel = lineage.enclosing_parcel(place)
    return parcel.pk if parcel is not None else None


def _ambiguity(members: list[Place], holder: Place | None) -> tuple[str, str] | None:
    """Why a stable-ref group must be reported rather than merged, or None when it may be merged."""
    if holder is not None and holder.status != PlaceStatus.CURRENT:
        return STABLE_REF_NOT_CURRENT, f"the stable ref is held by {holder.status} place {holder.pk}"
    aggregates = [member.pk for member in members if member.is_aggregate or (member.parent_id is not None and member.parent_relation == PlaceRelation.MEMBER_OF)]
    if aggregates:
        return AGGREGATE_MEMBER, f"place(s) {', '.join(map(str, aggregates))} take part in an earned aggregate"
    parcels = {pk for pk in (_parcel_id(member) for member in members) if pk is not None}
    if len(parcels) > 1:
        return DIFFERENT_PARCELS, f"members stand on parcels {', '.join(map(str, sorted(parcels)))}"
    return None


def _choose_survivor(members: list[Place], holder: Place | None) -> Place:
    """The place holding the stable ref; else the oldest with a wiki; else the oldest."""
    from urbanlens.dashboard.models.wiki.model import Wiki

    if holder is not None:
        return holder
    with_wiki = set(Wiki.objects.filter(place__in=members).values_list("place_id", flat=True))
    ordered = sorted(members, key=lambda member: member.pk)
    return next((member for member in ordered if member.pk in with_wiki), ordered[0])


def _source(place: Place) -> str:
    """Which provider a building place's record came from: its key's namespace (``overture``, ``osm``, ``cris``, ...)."""
    key = place.provider_key or ""
    return key.split(":", 1)[0] if ":" in key else place.provider


def _cross_source_pairs() -> list[tuple[int, int]]:
    """
    Current building places whose outlines hold each other's centroids, as id pairs (any source, filtered later).
    The same mutual-centroid test ``provisioning.find_matching_place`` uses, in one indexed self-join instead of a
    query per place.
    """
    sql = f"""
        SELECT a.id, b.id
        FROM {Place._meta.db_table} a
        JOIN {Place._meta.db_table} b ON a.id < b.id AND ST_Intersects(a.geometry, b.geometry)
        WHERE a.kind = %s AND b.kind = %s AND a.status = %s AND b.status = %s
          AND ST_Covers(a.geometry::geometry, ST_Centroid(b.geometry::geometry))
          AND ST_Covers(b.geometry::geometry, ST_Centroid(a.geometry::geometry))
        ORDER BY a.id, b.id
    """  # noqa: S608, SLF001 - the table name is the model's own, every value is a parameter
    with connection.cursor() as cursor:
        cursor.execute(sql, [PlaceKind.BUILDING, PlaceKind.BUILDING, PlaceStatus.CURRENT, PlaceStatus.CURRENT])
        return [(first, second) for first, second in cursor.fetchall()]


def _cross_source_findings(grouped: set[frozenset[int]]) -> list[Finding]:
    pairs = _cross_source_pairs()
    places = Place.objects.in_bulk({pk for pair in pairs for pk in pair})
    findings: list[Finding] = []
    for first, second in pairs:
        a, b = places[first], places[second]
        if _source(a) == _source(b) or any({first, second} <= group for group in grouped):
            continue
        same_parcel = _parcel_id(a) == _parcel_id(b)
        findings.append(
            Finding(
                CROSS_SOURCE_FOOTPRINT,
                (first, second),
                f"{a.provider_key or 'unkeyed'} and {b.provider_key or 'unkeyed'} hold each other's centroids ({'same parcel' if same_parcel else 'different parcels'}); footprint only",
            ),
        )
    return findings


def _placeless_wiki_findings() -> list[Finding]:
    from urbanlens.dashboard.models.pin.model import PinType
    from urbanlens.dashboard.models.wiki.model import Wiki

    findings: list[Finding] = []
    for wiki in Wiki.objects.filter(place__isnull=True, pin_type=PinType.BUILDING).select_related("location").order_by("pk"):
        for place_id, other_wiki_id in Place.objects.current().filter(kind=PlaceKind.BUILDING, geometry__intersects=wiki.location.point, wiki__isnull=False).values_list("pk", "wiki__pk").order_by("pk"):
            findings.append(Finding(PLACELESS_WIKI, (place_id,), f"wiki {wiki.pk} has no place but stands inside place {place_id}, whose wiki is {other_wiki_id}; geometry only", wikis=(wiki.pk, other_wiki_id)))
    return findings


def detect_duplicate_buildings(
    *,
    resolver: overture_refs.RedataResolver,
    fetch_buildings: overture_refs.BuildingsFetcher | None = None,
    locate_parcel: overture_refs.ParcelLocator | None = None,
) -> Detection:
    """
    Find duplicate building places: the groups safe to merge, and everything else worth a person's look.
    Read-only, apart from REData lookups.

    Args:
        resolver: Maps a legacy ref to its stable ref.
        fetch_buildings: A REData parcel's buildings, to recognise a hash REData still serves and to report footprint
            matches; None skips both.
        locate_parcel: REData's parcel at a coordinate, for a place whose parent is not a REData parcel.

    Returns:
        The mergeable groups, the findings, and the places REData could not answer for.
    """
    detection = Detection()
    by_ref: dict[str, list[Place]] = defaultdict(list)
    by_footprint: dict[str, list[Place]] = defaultdict(list)
    buildings_by_parcel: dict[str, list[dict[str, Any]] | None] = {}
    for place in overture_refs.legacy_keyed_places().current().select_related("parent").order_by("pk"):
        how, stable = _link(place, resolver=resolver, fetch_buildings=fetch_buildings, locate_parcel=locate_parcel, buildings_by_parcel=buildings_by_parcel)
        if how == _UNAVAILABLE:
            detection.unavailable.append(place.pk)
        elif how == _BY_REF and stable:
            by_ref[stable].append(place)
        elif how == _BY_FOOTPRINT and stable:
            by_footprint[stable].append(place)

    holders = {place.provider_key: place for place in overture_refs.redata_building_places().filter(provider_key__in=[*by_ref, *by_footprint]).select_related("parent")}
    grouped: set[frozenset[int]] = set()
    for stable in sorted(by_ref):
        holder = holders.get(stable)
        members = [*([holder] if holder is not None else []), *by_ref[stable]]
        if len(members) < 2:
            continue
        if problem := _ambiguity(members, holder):
            detection.findings.append(Finding(problem[0], tuple(member.pk for member in members), f"{stable}: {problem[1]}"))
            continue
        survivor = _choose_survivor(members, holder)
        detection.groups.append(DuplicateGroup(stable, survivor, [member for member in members if member.pk != survivor.pk]))
        grouped.add(frozenset(member.pk for member in members))

    for stable in sorted(by_footprint):
        others = [place for place in (holders.get(stable), *by_ref.get(stable, [])) if place is not None]
        members = others + by_footprint[stable]
        if len(members) >= 2:
            detection.findings.append(Finding(FOOTPRINT_ONLY, tuple(member.pk for member in members), f"{stable}: place(s) {', '.join(str(place.pk) for place in by_footprint[stable])} match it by footprint only"))

    detection.findings.extend(_cross_source_findings(grouped))
    detection.findings.extend(_placeless_wiki_findings())
    return detection


def plan_group(group: DuplicateGroup) -> dict[int, dict[str, Any]]:
    """
    What merging *group* would move, per loser, without writing anything.

    Args:
        group: A group from :func:`detect_duplicate_buildings`.

    Returns:
        Per loser pk, the counts the merge would move or deduplicate.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.external_tag import PlaceExternalTag
    from urbanlens.dashboard.models.place.model import PlaceAccessGrant
    from urbanlens.dashboard.models.wiki.model import Wiki

    survivor_wiki = Wiki.objects.filter(place=group.survivor).values_list("pk", flat=True).first()
    plans: dict[int, dict[str, Any]] = {}
    for loser in group.losers:
        loser_wiki = Wiki.objects.filter(place=loser).values_list("pk", flat=True).first()
        if loser_wiki is None:
            wiki = "none"
        elif survivor_wiki is None:
            wiki = f"wiki {loser_wiki} moves to the survivor"
            survivor_wiki = loser_wiki
        else:
            wiki = f"wiki {loser_wiki} merges into wiki {survivor_wiki}"
        plans[loser.pk] = {
            "locations": Location.objects.filter(place=loser).count(),
            "wiki": wiki,
            "access_grants": PlaceAccessGrant.objects.filter(place=loser).count(),
            "boundary_candidates": Boundary.objects.filter(place=loser).count(),
            "boundary_votes": BoundaryVote.objects.filter(place=loser).count(),
            "external_tags": PlaceExternalTag.objects.filter(place=loser).count(),
            "floorplans": Floorplan.objects.filter(place=loser).count(),
            "child_places": Place.objects.filter(parent=loser).count(),
        }
    return plans


def _merge_wikis(survivor: Place, loser: Place, moved: dict[str, Any], dropped: dict[str, Any]) -> None:
    """Hand the loser's wiki to the survivor, or merge it into the survivor's own."""
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.duplicate_wikis import merge_duplicate_wiki

    loser_wiki = Wiki.objects.filter(place=loser).first()
    if loser_wiki is None:
        return
    survivor_wiki = Wiki.objects.filter(place=survivor).first()
    if survivor_wiki is None:
        Wiki.objects.filter(pk=loser_wiki.pk).update(place=survivor)
        moved["wiki"] = {"moved": loser_wiki.pk}
        return
    result = merge_duplicate_wiki(survivor_wiki, loser_wiki)
    moved["wiki"] = {"merged_into": survivor_wiki.pk, **result.moved}
    if result.dropped:
        dropped["wiki"] = result.dropped


def _move_place_rows(survivor: Place, loser: Place, moved: dict[str, Any], dropped: dict[str, Any]) -> None:
    """Move the rows hanging off the loser place itself, the survivor's winning where a key is taken."""
    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.place.external_tag import PlaceExternalTag
    from urbanlens.dashboard.models.place.model import PlaceAccessGrant

    def drop(key: str, row: Any) -> None:
        dropped.setdefault(key, []).append(snapshot_row(row))
        row.delete()

    # Access is per domain, so a profile's grant on both places is one grant; theirs on the loser alone moves.
    grantees = set(PlaceAccessGrant.objects.filter(place=survivor).values_list("profile_id", flat=True))
    for grant in PlaceAccessGrant.objects.filter(place=loser).order_by("pk"):
        if grant.profile_id in grantees:
            drop("access_grants", grant)
        else:
            PlaceAccessGrant.objects.filter(pk=grant.pk).update(place=survivor)
            grantees.add(grant.profile_id)
            moved["access_grants"] = moved.get("access_grants", 0) + 1

    # One provider candidate per (type, source): the survivor's stands, and votes for the loser's follow it there.
    candidates = {(row.boundary_type, row.source): row for row in Boundary.objects.filter(place=survivor).exclude(source="")}
    for candidate in Boundary.objects.filter(place=loser).order_by("pk"):
        twin = candidates.get((candidate.boundary_type, candidate.source)) if candidate.source else None
        if twin is None:
            Boundary.objects.filter(pk=candidate.pk).update(place=survivor)
            moved["boundary_candidates"] = moved.get("boundary_candidates", 0) + 1
            continue
        BoundaryVote.objects.filter(boundary=candidate).update(boundary=twin)
        drop("boundary_candidates", candidate)

    # One boundary vote per profile per place: the survivor's stands.
    voters = set(BoundaryVote.objects.filter(place=survivor).values_list("profile_id", flat=True))
    for vote in BoundaryVote.objects.filter(place=loser).order_by("pk"):
        if vote.profile_id in voters:
            drop("boundary_votes", vote)
        else:
            BoundaryVote.objects.filter(pk=vote.pk).update(place=survivor)
            voters.add(vote.profile_id)
            moved["boundary_votes"] = moved.get("boundary_votes", 0) + 1

    for tag in PlaceExternalTag.objects.filter(place=loser).order_by("pk"):
        try:
            with transaction.atomic():
                PlaceExternalTag.objects.filter(pk=tag.pk).update(place=survivor)
            moved["external_tags"] = moved.get("external_tags", 0) + 1
        except IntegrityError:
            # The survivor already carries this exact (source, key, value).
            drop("external_tags", tag)

    if floorplans := Floorplan.objects.filter(place=loser).update(place=survivor):
        moved["floorplans"] = floorplans

    # Places parented on the loser (a wing on a building) re-parent through lineage, which repairs domains and counts.
    for child in Place.objects.filter(parent=loser).order_by("pk"):
        lineage.set_parent(child, survivor, child.parent_relation)
        moved.setdefault("child_places", []).append(child.pk)


def _merge_place(survivor: Place, loser: Place, group: DuplicateGroup) -> PlaceMerge:
    """
    Fold one loser into the survivor and mark it superseded.

    Returns:
        The audit record.
    """
    from urbanlens.dashboard.models.location.model import Location

    moved: dict[str, Any] = {}
    dropped: dict[str, Any] = {}
    old_key = loser.provider_key

    # Pins, audiences and wiki access all reach a building place through Location.place.
    location_ids = list(Location.objects.filter(place=loser).order_by("pk").values_list("pk", flat=True))
    Location.objects.filter(pk__in=location_ids).update(place=survivor, place_resolved_at=timezone.now())
    moved["locations"] = location_ids

    _merge_wikis(survivor, loser, moved, dropped)
    _move_place_rows(survivor, loser, moved, dropped)

    Place.objects.filter(pk=loser.pk).update(status=PlaceStatus.SUPERSEDED, provider_key="", updated=timezone.now())
    loser.status, loser.provider_key = PlaceStatus.SUPERSEDED, ""
    record = PlaceMerge.objects.create(survivor=survivor, loser=loser, gers_id=group.gers_id, old_keys=[f"{loser.provider}:{old_key}"] if old_key else [], moved=moved, dropped=dropped)
    logger.info("building merge: place %s merged into %s (%s)", loser.pk, survivor.pk, group.stable_ref)
    return record


def _settle_key(survivor: Place, stable_ref: str, records: list[PlaceMerge]) -> None:
    """File the survivor under the stable ref, and point every floorplan and swept-building record that named a loser at it."""
    if survivor.provider_key != stable_ref and overture_refs.rekey_place(survivor, stable_ref) is None:
        logger.warning("building merge: place %s could not take %s", survivor.pk, stable_ref)
    for record in records:
        floorplans = pins = 0
        for old in record.old_keys:
            counts = overture_refs.rewrite_building_refs(old.split(":", 1)[1], survivor.provider_key)
            floorplans, pins = floorplans + counts[0], pins + counts[1]
        if floorplans or pins:
            record.moved["building_refs"] = {"floorplans": floorplans, "pins": pins}
            record.save(update_fields=["moved", "updated"])


def _are_nested(first: Pin, second: Pin) -> bool:
    """Whether one pin sits beneath the other - a building pin under its own site pin is not a duplicate."""
    return first.pk in {ancestor.pk for ancestor in second.ancestor_chain()} or second.pk in {ancestor.pk for ancestor in first.ancestor_chain()}


def _suggest_pin_merges(survivor: Place, origins: dict[int, int]) -> list[int]:
    """
    Ask each owner left with a building pin from each side whether to merge them. Never merges a pin itself.

    Args:
        survivor: The place that stayed.
        origins: Every affected location's pk, mapped to the place it stood on before the merge.

    Returns:
        The suggestion ids.
    """
    from urbanlens.dashboard.models.pin.model import Pin, PinType
    from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion, PinMergeSuggestionOrigin

    by_profile: dict[int, list[Pin]] = defaultdict(list)
    # The types a person uses for "this building". An entrance or a hazard inside it is a different thing on the
    # building, never a duplicate of the building's own pin.
    for pin in Pin.objects.filter(location_id__in=origins, pin_type__in=(PinType.BUILDING, PinType.LOCATION_MARKER)).select_related("profile").order_by("pk"):
        by_profile[pin.profile_id].append(pin)
    suggestions: list[int] = []
    for pins in by_profile.values():
        anchor = next((pin for pin in pins if origins[pin.location_id] == survivor.pk), pins[0])
        for pin in pins:
            if origins[pin.location_id] == origins[anchor.location_id] or _are_nested(anchor, pin):
                continue
            suggestion = PinMergeSuggestion.objects.upsert(profile=pin.profile, pin_a=anchor, pin_b=pin, origin=PinMergeSuggestionOrigin.DUPLICATE_BUILDING, reason=_SUGGESTION_REASON)
            suggestions.append(suggestion.pk)
    return suggestions


def merge_group(group: DuplicateGroup) -> list[PlaceMerge]:
    """
    Merge every loser of *group* into its survivor, in one transaction.
    Idempotent: a loser already superseded, or already recorded as merged, is skipped, so a second run writes nothing.

    Args:
        group: A group from :func:`detect_duplicate_buildings`.

    Returns:
        One audit record per loser merged now.
    """
    from urbanlens.dashboard.models.location.model import Location

    with transaction.atomic():
        locked = {place.pk: place for place in Place.objects.select_for_update().filter(pk__in=[member.pk for member in group.members]).order_by("pk")}
        survivor = locked[group.survivor.pk]
        merged = set(PlaceMerge.objects.filter(loser_id__in=locked).values_list("loser_id", flat=True))
        losers = [locked[loser.pk] for loser in group.losers if locked[loser.pk].status == PlaceStatus.CURRENT and loser.pk not in merged]
        if not losers:
            return []
        origins = dict(Location.objects.filter(place_id__in=[survivor.pk, *(loser.pk for loser in losers)]).values_list("pk", "place_id"))
        records = [_merge_place(survivor, loser, group) for loser in losers]
        _settle_key(survivor, group.stable_ref, records)
        lineage.refresh_derived_flags([survivor.parent_id, *(loser.parent_id for loser in losers)])
        if suggestions := _suggest_pin_merges(survivor, origins):
            records[0].moved["pin_merge_suggestions"] = suggestions
            records[0].save(update_fields=["moved", "updated"])
    return records
