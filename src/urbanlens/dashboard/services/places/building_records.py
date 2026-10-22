"""
The buildings standing on a parcel: the record schema every source answers in, and the chain that asks them.

A source of building records contributes a :class:`BuildingRecordProvider` to ``points.BUILDING_RECORD_PROVIDERS``.
Core reads records only in the shape :class:`BuildingRecord` documents, so building lists, nesting, naming and dating
need know nothing of where a record came from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar, NotRequired, TypedDict

from urbanlens.dashboard.plugins.points import BUILDING_RECORD_PROVIDERS
from urbanlens.dashboard.services.core.gateway import is_source_outage

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: The keyless source the viewer's browser may be asked to fetch in place of the building-record chain's OpenStreetMap read.
OVERPASS = "overpass"


class BuildingSourceRef(TypedDict):
    """One source that references a building."""

    source: str


class BuildingRecord(TypedDict, total=False):
    """
    One physical building on a parcel, as a source of building records describes it.

    Every key is optional: a source fills what it knows. Records that describe one building together (the same
    footprint from several sources) are reconciled by the source into one record.

    Identity:
        ref: The source's id for the building, unique within one list.
        stable_ref: An id that survives the source's own reshaping; preferred over ``ref`` as the building's
            identity (see :func:`building_key`). Both are opaque to core.
        parent_ref: The ``ref`` of the record that contains this building. A coarse footprint enclosing finer ones
            is their parent, and a verified "these are different buildings": a wing is not the block it is part of.
        child_refs: The ``ref`` of every record this one contains.
        overlap_refs: The ``ref`` of records the source could not tell apart from this one. A record with any is not
            acted on without asking.
    What it is:
        name, building_number: As the source gives them; either may be blank.
        year_built: A construction year.
        year_built_basis: What ``year_built`` dates: ``"building"`` for the building's own year. Anything else, or
            nothing, is the parcel's one year, which says nothing of this building (see ``services.pins.build_dates``).
        is_on_property: False for a record the source returned for the search area that is not on this parcel.
    Where it is:
        latitude, longitude: A point on the building.
        geometry: A GeoJSON footprint; a ``Point`` or missing geometry means no outline is known.
    Provenance:
        sources: Every source that references the building, richest first, as ``{"source": key, ...}``.
        source: The one source of a flat record.
    """

    ref: str
    stable_ref: str
    parent_ref: str
    child_refs: list[str]
    overlap_refs: list[str]
    name: str
    building_number: str
    year_built: NotRequired[int | str | None]
    year_built_basis: NotRequired[str | None]
    is_on_property: NotRequired[bool]
    latitude: NotRequired[float | None]
    longitude: NotRequired[float | None]
    geometry: NotRequired[dict[str, Any] | None]
    sources: NotRequired[list[BuildingSourceRef]]
    source: NotRequired[str]


def building_key(building: dict[str, Any]) -> str:
    """The id a building record is known by: its ``stable_ref`` when it has one, else its ``ref``."""
    return str(building.get("stable_ref") or building.get("ref") or "").strip()


@dataclass(slots=True)
class BuildingsAnswer:
    """
    What one provider found on a parcel.

    Attributes:
        buildings: The records, empty when it found none.
        unanswered_sources: Sources the provider asked and could not hear from, so the list is a floor.
        browser_fetch: Why the viewer's browser should fetch the keyless source ``overpass`` itself - its budget is nearly
            spent though it did answer - or ``""``. A source in ``unanswered_sources`` asks the browser too.
    """

    buildings: list[dict[str, Any]]
    unanswered_sources: tuple[str, ...] = ()
    browser_fetch: str = ""


class BuildingRecordProvider(ABC):
    """
    A source of the building records on a location's parcel.

    A *primary* provider answers the whole list (:meth:`buildings_on_parcel`). A *supplement* adds records to a
    primary's list when that primary :attr:`accepts_supplements`, and stands in for the list when no primary has one.
    """

    #: Names the provider in a payload's ``unanswered_sources`` and in the log.
    key: ClassVar[str]
    #: Whether the provider answers the whole list (:meth:`buildings_on_parcel`); False for a supplement.
    primary: ClassVar[bool] = True
    #: Recorded as the ``provider`` of a list this provider answered.
    tag: ClassVar[str]
    #: The ``Place.provider`` namespace of building places made from this provider's records; blank for none.
    place_namespace: ClassVar[str] = ""
    #: Whether supplements are folded into this provider's list.
    accepts_supplements: ClassVar[bool] = False
    #: Whether the background cycle may ask this provider for every location, which only a provider cheap for the
    #: operator to ask should allow.
    bulk_enrichment: ClassVar[bool] = False
    #: How the ``source`` keys this provider's records carry are described to people.
    source_labels: ClassVar[dict[str, str]] = {}
    #: The rate-limiter service keys one question to this provider spends.
    service_keys: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider can be asked at all on this install."""

    def buildings_on_parcel(self, location: Location) -> BuildingsAnswer:
        """
        Every building on the location's parcel.

        Args:
            location: The location whose parcel to enumerate.

        Returns:
            The answer; a supplement (``primary`` False) is never asked.

        Raises:
            UpstreamBusyError: ``answer_pending`` is set when the provider is working out the list and named when it
                will have it, so no other provider stands in for it.
            Exception: The provider could not be asked (see ``is_source_outage``).
        """
        return BuildingsAnswer([])

    def supplementary_records(self, location: Location) -> list[dict[str, Any]]:
        """
        Records this provider adds to another provider's list.

        Raises:
            Exception: The provider could not be asked (see ``is_source_outage``).
        """
        return []

    def merge_supplement(self, buildings: list[dict[str, Any]], supplement: list[dict[str, Any]], polygon: GEOSGeometry | None) -> list[dict[str, Any]]:
        """
        Fold this provider's records into ``buildings``.

        Args:
            buildings: The records of the primary list, possibly empty.
            supplement: What :meth:`supplementary_records` returned.
            polygon: The parcel outline; None when it has none.

        Returns:
            The merged list; the plain concatenation unless the provider knows better.
        """
        return [*buildings, *supplement]

    def place_keyer(self) -> Callable[[dict[str, Any], GEOSGeometry | None, str], str] | None:
        """
        A function that rekeys a record's place key before a building place is filed under it, or None to keep the key.

        One call per import, so it may memoize what it learns.
        """
        return None


def providers() -> list[BuildingRecordProvider]:
    """The providers that can be asked, in priority order."""
    return [provider for provider in BUILDING_RECORD_PROVIDERS if provider.available()]


def place_provider(namespace: str) -> BuildingRecordProvider | None:
    """The provider whose building places are filed under ``namespace``."""
    return next((provider for provider in BUILDING_RECORD_PROVIDERS if namespace and provider.place_namespace == namespace), None)


def preferred_place_namespace() -> str:
    """The ``Place.provider`` namespace building places are filed under: that of the highest-priority provider that has one."""
    return next((provider.place_namespace for provider in BUILDING_RECORD_PROVIDERS if provider.place_namespace), "")


def source_labels() -> dict[str, str]:
    """How every provider describes the ``source`` keys its records carry."""
    labels: dict[str, str] = {}
    for provider in BUILDING_RECORD_PROVIDERS:
        labels.update(provider.source_labels)
    return labels


def bulk_enrichment_available() -> bool:
    """Whether any provider the background cycle may ask for every location can be asked."""
    return any(provider.bulk_enrichment and provider.available() for provider in BUILDING_RECORD_PROVIDERS)


def service_keys() -> tuple[str, ...]:
    """The rate-limiter service keys the providers that can be asked spend, without repeats."""
    return tuple(dict.fromkeys(key for provider in providers() for key in provider.service_keys))


@dataclass(slots=True)
class _Run:
    """One resolution: the outages and partial answers noted so far."""

    partial: list[str] = field(default_factory=list)
    outages: dict[str, Exception] = field(default_factory=dict)
    browser_reason: str = ""

    def asked[T](self, provider: BuildingRecordProvider, ask: Callable[[], T], default: T) -> T:
        """What ``ask`` returns, or ``default`` with the provider's outage noted; any other failure propagates."""
        try:
            return ask()
        except Exception as exc:
            if getattr(exc, "answer_pending", False) or not is_source_outage(exc):
                raise
            self.outages[provider.key] = exc
            return default

    def marked(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The payload, marked partial when a source was unheard from, so the cache keeps it only briefly."""
        from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY
        from urbanlens.dashboard.services.pins.external_data import browser_fetch_marker

        unanswered = [*self.partial, *self.outages]
        marked = {**payload, UNANSWERED_SOURCES_KEY: unanswered} if unanswered else payload
        reason = self.browser_reason or ("unavailable" if OVERPASS in unanswered else "")
        return {**marked, **browser_fetch_marker(OVERPASS, reason)} if reason else marked


def fetch_parcel_buildings(location: Location) -> dict[str, Any]:
    """
    Resolve every building on a location's parcel, from the first provider that finds any.

    Args:
        location: The location whose parcel to enumerate.

    Returns:
        ``{"buildings": [...], "provider": <the answering provider's tag>}``, or ``{}`` when every provider answered
        and none found anything. When a provider named a source it could not hear from, or one could not be reached,
        whatever is returned carries ``"unanswered_sources"``, so the cache keeps it only briefly.

    Raises:
        UpstreamBusyError: A provider is working out the list and named when it will have it (``answer_pending``), so
            no fallback stands in for it and the caller asks again after the wait.
        Exception: A provider could not be asked and no other found anything, so there is no answer to cache (see
            ``is_source_outage``).
    """
    run = _Run()
    asked = providers()
    for provider in (provider for provider in asked if provider.primary):
        answer = run.asked(provider, lambda provider=provider: provider.buildings_on_parcel(location), BuildingsAnswer([]))
        run.partial.extend(answer.unanswered_sources)
        run.browser_reason = run.browser_reason or answer.browser_fetch
        if answer.buildings:
            return run.marked({"buildings": _with_supplements(provider, answer.buildings, asked, location, run), "provider": provider.tag})

    supplemented = _supplements_alone(asked, location, run)
    if supplemented is not None:
        return run.marked(supplemented)
    if run.outages:
        raise next(iter(run.outages.values()))
    return run.marked({"buildings": []}) if run.partial or run.browser_reason else {}


def _parcel_polygon(location: Location) -> GEOSGeometry | None:
    from urbanlens.dashboard.services.places.scope import parcel_polygon_for_location

    return parcel_polygon_for_location(location)


def _supplement_records(supplier: BuildingRecordProvider, location: Location, run: _Run) -> list[dict[str, Any]]:
    return run.asked(supplier, lambda: supplier.supplementary_records(location), [])


def _with_supplements(winner: BuildingRecordProvider, buildings: list[dict[str, Any]], asked: list[BuildingRecordProvider], location: Location, run: _Run) -> list[dict[str, Any]]:
    """``buildings`` with every supplement folded in, when the provider that found them takes any."""
    if not winner.accepts_supplements:
        return buildings
    for supplier in asked:
        if supplier is winner:
            continue
        if records := _supplement_records(supplier, location, run):
            buildings = supplier.merge_supplement(buildings, records, _parcel_polygon(location))
    return buildings


def _supplements_alone(asked: list[BuildingRecordProvider], location: Location, run: _Run) -> dict[str, Any] | None:
    """The list a supplement makes when no primary found a building, or None."""
    for supplier in asked:
        if records := _supplement_records(supplier, location, run):
            merged = supplier.merge_supplement([], records, _parcel_polygon(location))
            if merged:
                return {"buildings": merged, "provider": supplier.tag}
    return None
