"""Moving building places off Overture's legacy content-hash refs, onto ``overture:<gers_id>``.

REData served an Overture building's ``ref`` as a hash of its name and position, which changes whenever an Overture
release reshapes or renames the building (REData P98). Places keyed on it, and floorplans and swept-building records
that name it, drift with it. REData now serves ``stable_ref``, the ref each building keeps, and
``/buildings/resolve/``, which maps a hash it served to that ref.

Transitional: once no legacy ref remains (``rekey_overture_places --check``) and REData has switched ``ref`` itself,
everything here but :func:`building_key` goes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.place.model import Place, PlaceKind

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.place.queryset import PlaceQuerySet

logger = logging.getLogger(__name__)

#: The provider namespace REData's places are filed under.
REDATA = "redata"
#: Every Overture ref starts with this.
OVERTURE_PREFIX = "overture:"
#: A legacy Overture ref: 12 hex digits of a content hash, ``#n`` when repeated in one answer. A ``gers_id`` is 32.
LEGACY_OVERTURE_REF = re.compile(r"^overture:[0-9a-f]{12}(#\d+)?$")

#: Maps a legacy ref to its stable ref, or None when it cannot.
Resolver = Callable[[str], str | None]


def is_legacy_overture_ref(ref: str) -> bool:
    """Whether ``ref`` is an Overture content-hash ref."""
    return bool(LEGACY_OVERTURE_REF.match(ref or ""))


def building_key(building: dict[str, Any]) -> str:
    """The ref a REData building record is keyed on: its ``stable_ref`` when REData serves one, else its ``ref``."""
    return str(building.get("stable_ref") or building.get("ref") or "").strip()


def redata_building_places() -> PlaceQuerySet:
    """Every building place keyed on a REData ref."""
    return Place.objects.filter(provider=REDATA, kind=PlaceKind.BUILDING)


def legacy_keyed_places() -> PlaceQuerySet:
    """Building places still keyed on a legacy Overture ref."""
    return redata_building_places().filter(provider_key__regex=LEGACY_OVERTURE_REF.pattern)


class RedataResolver:
    """A :data:`Resolver` over REData's ``/buildings/resolve/``, memoized for one run.

    Any failure reads as "cannot resolve", and stops further calls for the rest of the run: a REData that does not
    serve the endpoint yet answers every ref the same way.
    """

    def __init__(self) -> None:
        """Start with nothing resolved."""
        self._answers: dict[str, dict[str, Any] | None] = {}
        self._unavailable = False

    def answer(self, ref: str) -> dict[str, Any] | None:
        """REData's full answer for ``ref``, or None when it could not be asked.

        Args:
            ref: The ref to resolve.

        Returns:
            ``{ref, status, stable_ref, candidates}``, or None.
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        if ref in self._answers:
            return self._answers[ref]
        if self._unavailable:
            return None
        try:
            answer: dict[str, Any] | None = RedataGateway().resolve_building_ref(ref)
        except PropertyRecordsUnavailableError:
            logger.info("overture refs: REData could not resolve %s; not asking again this run", ref, exc_info=True)
            self._unavailable = True
            answer = None
        self._answers[ref] = answer
        return answer

    def __call__(self, ref: str) -> str | None:
        """The stable ref ``ref`` resolves to, or None.

        Args:
            ref: The ref to resolve.

        Returns:
            ``overture:<gers_id>``, or None when it is ambiguous, unknown or REData could not be asked.
        """
        answer = self.answer(ref)
        stable = answer.get("stable_ref") if answer and answer.get("status") == "resolved" else None
        return stable if isinstance(stable, str) and stable else None


def _resolve(ref: str, resolver: Resolver | None) -> str | None:
    """``ref``'s stable form: itself when already stable, else what ``resolver`` says."""
    if not ref.startswith(OVERTURE_PREFIX):
        return None
    if not is_legacy_overture_ref(ref):
        return ref
    return resolver(ref) if resolver is not None else None


@dataclass(frozen=True, slots=True)
class Rekeyed:
    """What one re-key moved."""

    place: int
    old: str
    new: str
    floorplans: int
    pins: int


def rekey_place(place: Place, new_key: str) -> Rekeyed | None:
    """File ``place`` under ``new_key``, with every floorplan and swept-building record that named its old key.

    Never deletes, and never takes a key another place holds: that is a duplicate to merge, not a re-key.

    Args:
        place: The place to move.
        new_key: Its stable ref.

    Returns:
        What moved, or None when another place already holds ``new_key`` or the key is unchanged.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.pin.model import Pin

    old_key = place.provider_key
    if not old_key or old_key == new_key:
        return None
    with transaction.atomic():
        if Place.objects.filter(provider=place.provider, provider_key=new_key, kind=place.kind).exclude(pk=place.pk).exists():
            return None
        Place.objects.filter(pk=place.pk).update(provider_key=new_key, updated=timezone.now())
        floorplans = Floorplan.objects.filter(building_ref=old_key).update(building_ref=new_key)
        pins = 0
        for pin in Pin.objects.filter(auto_nested_buildings__contains=[{"ref": old_key}]).only("pk", "auto_nested_buildings").select_for_update():
            entries = [({**entry, "ref": new_key} if isinstance(entry, dict) and entry.get("ref") == old_key else entry) for entry in pin.auto_nested_buildings]
            Pin.objects.filter(pk=pin.pk).update(auto_nested_buildings=entries)
            pins += 1
    place.provider_key = new_key
    logger.info("overture refs: place %s re-keyed %s -> %s (%d floorplans, %d pins)", place.pk, old_key, new_key, floorplans, pins)
    return Rekeyed(place=place.pk, old=old_key, new=new_key, floorplans=floorplans, pins=pins)


def key_for_building(building: dict[str, Any], footprint: GEOSGeometry | None, *, resolver: Resolver | None) -> str:
    """The key to file a REData building under, re-keying the place that already stands for it if that place is still
    on a legacy Overture ref.

    The existing place is found by the building's own legacy ``ref``, or, when the hash has drifted since, among the
    current legacy-keyed building places overlapping ``footprint`` whose ref resolves to the same building. A record
    from an answer REData served before ``stable_ref`` existed is filed under its resolved stable ref when it has one.

    Args:
        building: One REData building record.
        footprint: Its footprint, when it has one.
        resolver: Maps a legacy ref to its stable ref; None never asks REData.

    Returns:
        The key, possibly empty for a record with no ref.
    """
    key = building_key(building)
    if not key.startswith(OVERTURE_PREFIX):
        return key
    places = redata_building_places()
    if places.filter(provider_key=key).exists():
        return key

    legacy = str(building.get("ref") or "").strip()
    stable = key if not is_legacy_overture_ref(key) else _resolve(key, resolver)
    if stable is None:
        return key
    if places.filter(provider_key=stable).exists():
        return stable
    if legacy != stable and is_legacy_overture_ref(legacy) and (held := places.filter(provider_key=legacy).first()) is not None and rekey_place(held, stable):
        return stable
    if footprint is not None:
        for candidate in legacy_keyed_places().current().filter(geometry__intersects=footprint).exclude(provider_key=legacy).order_by("pk"):
            if _resolve(candidate.provider_key, resolver) == stable and rekey_place(candidate, stable):
                return stable
    return stable


#: Fetches a REData parcel's buildings by its uuid; raises ``PropertyRecordsUnavailableError`` when it cannot.
BuildingsFetcher = Callable[[str], list[dict[str, Any]]]

#: Outcomes of :func:`migrate_legacy_refs`, per place.
RESOLVED = "resolved"
FOOTPRINT = "footprint"
DUPLICATE = "duplicate"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no_match"
UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class LegacyPlace:
    """What became, or would become, of one legacy-keyed place."""

    place: int
    old: str
    outcome: str
    new: str = ""
    detail: str = ""


@dataclass(slots=True)
class MigrationReport:
    """Everything :func:`migrate_legacy_refs` found."""

    places: list[LegacyPlace]
    floorplans: dict[str, str]
    pins: dict[str, str]

    def outcomes(self) -> dict[str, int]:
        """How many places ended in each outcome."""
        counts: dict[str, int] = {}
        for row in self.places:
            counts[row.outcome] = counts.get(row.outcome, 0) + 1
        return counts


def _footprint_match(place: Place, buildings: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """The stable Overture refs among ``buildings`` whose footprint and ``place``'s outline each hold the other's centroid."""
    from urbanlens.dashboard.services.pins.pin_restructure import building_footprint

    if place.geometry is None:
        return NO_MATCH, []
    outline = place.geometry
    matches: list[str] = []
    for building in buildings:
        key = building_key(building)
        if not key.startswith(OVERTURE_PREFIX) or is_legacy_overture_ref(key):
            continue
        footprint = building_footprint(building)
        if footprint is not None and footprint.contains(outline.centroid) and outline.contains(footprint.centroid):
            matches.append(key)
    matches = sorted(set(matches))
    return (FOOTPRINT if len(matches) == 1 else AMBIGUOUS if matches else NO_MATCH), matches


def _classify(place: Place, *, resolver: RedataResolver, fetch_buildings: BuildingsFetcher | None, buildings_by_parcel: dict[str, list[dict[str, Any]] | None]) -> LegacyPlace:
    """Where one legacy-keyed place should move, by REData's resolve and then by footprint."""
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

    row = LegacyPlace(place=place.pk, old=place.provider_key, outcome=NO_MATCH)
    answer = resolver.answer(place.provider_key)
    status = answer.get("status") if answer else None
    if answer is None:
        row.outcome = UNAVAILABLE
        return row
    if status == "resolved" and (stable := resolver(place.provider_key)):
        row.outcome, row.new = RESOLVED, stable
    elif status == "ambiguous":
        row.outcome, row.detail = AMBIGUOUS, ", ".join(str(candidate) for candidate in answer.get("candidates") or [])
        return row
    else:
        parcel = place.parent
        if fetch_buildings is None or parcel is None or parcel.provider != REDATA or not parcel.provider_key:
            row.detail = "no REData parcel to match the footprint against"
            return row
        if parcel.provider_key not in buildings_by_parcel:
            try:
                buildings_by_parcel[parcel.provider_key] = fetch_buildings(parcel.provider_key)
            except PropertyRecordsUnavailableError:
                logger.info("overture refs: no buildings for parcel %s", parcel.provider_key, exc_info=True)
                buildings_by_parcel[parcel.provider_key] = None
        buildings = buildings_by_parcel[parcel.provider_key]
        if buildings is None:
            row.outcome = UNAVAILABLE
            return row
        row.outcome, matches = _footprint_match(place, buildings)
        if row.outcome != FOOTPRINT:
            row.detail = ", ".join(matches)
            return row
        row.new = matches[0]
    if row.new and redata_building_places().filter(provider_key=row.new).exclude(pk=place.pk).exists():
        row.outcome, row.detail = DUPLICATE, f"{row.new} is already another place's key"
    return row


def migrate_legacy_refs(*, apply: bool, resolver: RedataResolver, fetch_buildings: BuildingsFetcher | None) -> MigrationReport:
    """Re-key every building place, floorplan and swept-building record still on a legacy Overture ref.

    A place moves to the stable ref REData resolves its key to. A key REData never recorded (served before its
    2026-10-11 release) is matched by footprint against the parent parcel's current buildings: the one stable
    Overture building whose footprint and the place's outline each hold the other's centroid. More than one match is
    ambiguous, and a stable ref another place already holds is a duplicate for ``merge_duplicate_building_places``;
    both leave the place unchanged, as does a REData that cannot answer. Nothing is deleted, and running it again
    re-keys only what is still legacy.

    Args:
        apply: Write; otherwise only report.
        resolver: Maps a legacy ref to its stable ref.
        fetch_buildings: A REData parcel's buildings, for the footprint match; None skips it.

    Returns:
        Each legacy place's outcome, and the floorplan and swept-building refs that were (or would be) moved.
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.pin.model import Pin

    buildings_by_parcel: dict[str, list[dict[str, Any]] | None] = {}
    rows: list[LegacyPlace] = []
    for place in legacy_keyed_places().select_related("parent").order_by("pk"):
        row = _classify(place, resolver=resolver, fetch_buildings=fetch_buildings, buildings_by_parcel=buildings_by_parcel)
        if apply and row.outcome in (RESOLVED, FOOTPRINT) and rekey_place(place, row.new) is None:
            row.outcome, row.detail = DUPLICATE, f"{row.new} was taken while this ran"
        rows.append(row)

    # Floorplans and swept buildings naming a legacy ref no place holds any more, or never did.
    floorplans: dict[str, str] = {}
    for ref in sorted(set(Floorplan.objects.filter(building_ref__regex=LEGACY_OVERTURE_REF.pattern).values_list("building_ref", flat=True))):
        if stable := resolver(ref):
            floorplans[ref] = stable
            if apply:
                Floorplan.objects.filter(building_ref=ref).update(building_ref=stable)
    pins: dict[str, str] = {}
    for pin in Pin.objects.exclude(auto_nested_buildings=[]).only("pk", "auto_nested_buildings").order_by("pk").iterator():
        entries = pin.auto_nested_buildings or []
        legacy = {ref for entry in entries if isinstance(entry, dict) and is_legacy_overture_ref(ref := str(entry.get("ref") or ""))}
        moves = {ref: stable for ref in legacy if (stable := resolver(ref))}
        pins.update(moves)
        if apply and moves:
            Pin.objects.filter(pk=pin.pk).update(auto_nested_buildings=[({**entry, "ref": moves[entry["ref"]]} if isinstance(entry, dict) and entry.get("ref") in moves else entry) for entry in entries])
    return MigrationReport(places=rows, floorplans=floorplans, pins=pins)


def legacy_refs_remaining() -> dict[str, int]:
    """How many legacy Overture refs each store still holds: the "zero references remain" check of REData's P98."""
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.pin.model import Pin

    swept = sum(1 for entries in Pin.objects.exclude(auto_nested_buildings=[]).values_list("auto_nested_buildings", flat=True).iterator() for entry in entries or [] if isinstance(entry, dict) and is_legacy_overture_ref(str(entry.get("ref") or "")))
    return {
        "Place.provider_key": Place.objects.filter(provider_key__regex=LEGACY_OVERTURE_REF.pattern).count(),
        "Floorplan.building_ref": Floorplan.objects.filter(building_ref__regex=LEGACY_OVERTURE_REF.pattern).count(),
        "Pin.auto_nested_buildings": swept,
    }


__all__ = [
    "AMBIGUOUS",
    "DUPLICATE",
    "FOOTPRINT",
    "LEGACY_OVERTURE_REF",
    "NO_MATCH",
    "OVERTURE_PREFIX",
    "REDATA",
    "RESOLVED",
    "UNAVAILABLE",
    "BuildingsFetcher",
    "LegacyPlace",
    "MigrationReport",
    "RedataResolver",
    "Rekeyed",
    "Resolver",
    "building_key",
    "is_legacy_overture_ref",
    "key_for_building",
    "legacy_keyed_places",
    "legacy_refs_remaining",
    "migrate_legacy_refs",
    "redata_building_places",
    "rekey_place",
]
