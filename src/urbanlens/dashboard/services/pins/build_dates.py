"""When a pinned place was built, read from the building, register and parcel records already cached for it.

Nothing here calls an upstream. A year is written only where nobody has set a date, so an owner's own date always wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Mapping

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.building_clusters import BuildingCluster
    from urbanlens.dashboard.services.pins.pin_restructure import BuildingNester

logger = logging.getLogger(__name__)

#: The ``services.facts.registry`` key a wiki's build year is recorded under.
BUILT_YEAR_FACT = "built_year"
#: Earliest year accepted from a record; anything before it is a parse error, not a building.
EARLIEST_YEAR = 1000

#: ``FactEvidence.source_name`` per source, also naming where a pin's date came from in the logs.
SOURCE_BUILDING_RECORD = "building_record"
SOURCE_HISTORIC_REGISTER = "historic_register"
SOURCE_PROPERTY_RECORD = "property_record"

#: REData's ``year_built_basis`` for a year a source gave the building itself, such as a CRIS survey date.
YEAR_BASIS_BUILDING = "building"


@dataclass(frozen=True, slots=True)
class BuildYear:
    """A year something was built, and the record that says so."""

    year: int
    source: str

    @property
    def as_date(self) -> date:
        """January 1st of the year, the convention for a year-only date (as ``services.ai.link_extraction`` stores one)."""
        return date(self.year, 1, 1)


def plausible_year(value: Any) -> int | None:
    """A construction year from a record's field, or None when it is missing or impossible.

    Args:
        value: The raw ``year_built``: an int, or text such as ``"1871"``.

    Returns:
        The year.
    """
    if isinstance(value, bool):
        return None
    try:
        year = int(str(value).strip()[:4]) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return year if EARLIEST_YEAR <= year <= timezone.localdate().year else None


def own_build_year(record: Mapping[str, Any]) -> int | None:
    """The year a building record says that building itself was built.

    REData's ``year_built_basis`` says what a ``year_built`` dates. ``"building"`` is the building's own year.
    ``"parcel"`` is the assessor's one year for the parcel's principal improvement, which REData sets on at most the
    building under the point the parcel was looked up at, so it says nothing of when that building was built. A record
    with no basis (from REData before 0.3.6, a list cached before then, or Overpass) may hold either, so it is read as
    the parcel's.

    Args:
        record: A building record, reconciled or one source's.

    Returns:
        The year, or None when the record gives none of the building's own.
    """
    if record.get("year_built_basis") != YEAR_BASIS_BUILDING:
        return None
    return plausible_year(record.get("year_built"))


def cluster_build_year(cluster: BuildingCluster) -> int | None:
    """The year one physical building was built, from the first of its records that dates it (:func:`own_build_year`).

    Args:
        cluster: The building's records.

    Returns:
        The year, or None.
    """
    return next((year for member in cluster.members if (year := own_build_year(member)) is not None), None)


def site_build_year(pin: Pin, nester: BuildingNester) -> BuildYear | None:
    """The best year for what a root pin stands for, most specific record first.

    A register listing drawn around the pin names the place itself. Failing that, the building the pin stands on is
    what its owner pointed at, when its records date it. The parcel record's year is last, and only for a pin standing
    for the property: it is the assessor's one year for the parcel's principal improvement, so it dates no particular
    building, and is often a placeholder.

    Args:
        pin: A root pin.
        nester: Its property's buildings.

    Returns:
        The year and its source, or None when nothing cached says.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.services.locations.register_names import CONTAINS_POINT_KEY

    location = pin.location
    registers = LocationCache.get_fresh(location, "redata_historic_registers")
    for row in (registers.data.get("resources") if registers and isinstance(registers.data, dict) else None) or []:
        if isinstance(row, dict) and row.get(CONTAINS_POINT_KEY) and (year := plausible_year(row.get("year_built"))) is not None:
            return BuildYear(year, SOURCE_HISTORIC_REGISTER)

    if (year := standing_year(pin, nester)) is not None:
        return BuildYear(year, SOURCE_BUILDING_RECORD)

    if not parcel_year_may_date(pin):
        return None
    record = LocationCache.get_fresh(location, "property_records")
    if record and isinstance(record.data, dict) and record.data.get("available") and (year := plausible_year(record.data.get("year_built"))) is not None:
        return BuildYear(year, SOURCE_PROPERTY_RECORD)
    return None


def standing_year(pin: Pin, nester: BuildingNester) -> int | None:
    """The own year of the innermost building the pin stands on that has one (:func:`standing_on`, :func:`cluster_build_year`).

    Args:
        pin: The pin.
        nester: Its property's buildings.

    Returns:
        The year, or None.
    """
    return next((found for cluster in standing_on(pin, nester) if (found := cluster_build_year(cluster)) is not None), None)


def parcel_year_may_date(marker: Pin | Wiki) -> bool:
    """Whether the parcel record's year may date a pin or wiki: not one that reads as a building.

    A campus pin standing on its main building's place reads as that building, and the assessor's year for the
    parcel's principal improvement is not known to be that building's.

    Args:
        marker: The pin or wiki.

    Returns:
        False when ``marker`` reads as a building.
    """
    from urbanlens.dashboard.models.pin.model import PinType
    from urbanlens.dashboard.services.places.scope import effective_pin_type

    return effective_pin_type(marker) != PinType.BUILDING


def standing_on(pin: Pin, nester: BuildingNester) -> list[BuildingCluster]:
    """The buildings a pin stands on, innermost first: those whose footprint holds it, else a footprintless one at it.

    Args:
        pin: The pin.
        nester: Its property's buildings.

    Returns:
        The buildings, possibly none.
    """
    latitude, longitude = pin.effective_latitude, pin.effective_longitude
    if latitude is None or longitude is None:
        return []
    point = (float(latitude), float(longitude))
    holding = [cluster for cluster in nester.clusters if cluster.holding_footprint(*point) is not None]
    if holding:
        return sorted(holding, key=lambda cluster: cluster.depth, reverse=True)
    # A record published as a point has no footprint to stand in, only a marker to stand at.
    return [cluster for cluster in nester.clusters if cluster.footprint is None and cluster.covers(*point)]


def fill_build_dates(pin: Pin) -> int:
    """Date a root pin and its building pins from the records cached for its property, where nobody has.

    Also records each year on the community wikis standing for them, for anyone whose community features are on.

    Args:
        pin: A root pin.

    Returns:
        How many pins were dated.
    """
    from urbanlens.dashboard.models.pin.model import Pin as PinModel
    from urbanlens.dashboard.services.pins.building_clusters import match_clusters
    from urbanlens.dashboard.services.pins.pin_restructure import BuildingNester, building_markers

    if pin.location_id is None:
        return 0
    nester = BuildingNester.for_pin(pin)
    dated = 0

    matched, _unmatched = match_clusters(nester.clusters, building_markers(pin.descendants().select_related("location")))
    yearly: list[tuple[PinModel, BuildYear]] = []
    for index, marker in matched.items():
        if isinstance(marker, PinModel) and (year := cluster_build_year(nester.clusters[index])) is not None:
            yearly.append((marker, BuildYear(year, SOURCE_BUILDING_RECORD)))
    if (site_year := site_build_year(pin, nester)) is not None:
        yearly.append((pin, site_year))

    for marker, built in yearly:
        # A conditional update, so a date the owner sets meanwhile is never overwritten.
        if PinModel.objects.filter(pk=marker.pk, date_built__isnull=True).update(date_built=built.as_date, updated=timezone.now()):
            dated += 1
    if pin.profile.community_enabled:
        _record_wiki_years(pin, nester, site_year)
    return dated


def _record_wiki_years(pin: Pin, nester: BuildingNester, site_year: BuildYear | None) -> None:
    """Record the campus's and each building's year on the wikis standing for them.

    A building record's year is taken back only on a building list REData answered in full: with no list cached, or
    one a source did not answer, a missing year says nothing.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.locations.site_scope import cached_parcel_buildings
    from urbanlens.dashboard.services.pins.building_clusters import match_clusters
    from urbanlens.dashboard.services.pins.pin_restructure import building_markers

    campus = Wiki.objects.filter(location_id=pin.location_id).first()
    if campus is None:
        return
    if site_year is not None and (site_year.source != SOURCE_PROPERTY_RECORD or parcel_year_may_date(campus)):
        record_wiki_build_year(campus, site_year)
    cached = cached_parcel_buildings(pin.location)
    settled = cached is not None and cached.complete
    # A record that no longer dates the campus takes back what it said, such as a parcel's year recorded as a
    # building's before REData said which it was.
    if settled and standing_year(pin, nester) is None:
        retract_wiki_build_year(campus, SOURCE_BUILDING_RECORD)
    if not parcel_year_may_date(campus):
        retract_wiki_build_year(campus, SOURCE_PROPERTY_RECORD)
    matched, _unmatched = match_clusters(nester.clusters, building_markers(campus.descendants().select_related("location")))
    for index, wiki in matched.items():
        if not isinstance(wiki, Wiki):
            continue
        if (year := cluster_build_year(nester.clusters[index])) is not None:
            record_wiki_build_year(wiki, BuildYear(year, SOURCE_BUILDING_RECORD))
        elif settled:
            retract_wiki_build_year(wiki, SOURCE_BUILDING_RECORD)


def record_wiki_build_year(wiki: Wiki, built: BuildYear) -> bool:
    """Record a record's year on a wiki's ``built_year`` fact, once per source.

    Re-reading a record that still says the same thing adds nothing; one that now says something else replaces that
    source's earlier observation rather than outvoting it.

    Args:
        wiki: The wiki the year is about.
        built: The year and the record it came from.

    Returns:
        Whether a new observation was recorded.
    """
    from urbanlens.dashboard.models.facts.model import FactEvidence, FactSourceKind
    from urbanlens.dashboard.services.facts.evidence import record_evidence

    earlier = FactEvidence.objects.active().filter(fact__wiki=wiki, fact__key=BUILT_YEAR_FACT, source_kind=FactSourceKind.EXTERNAL_SOURCE, source_name=built.source)
    if any(row.value_number == built.year for row in earlier):
        return False
    earlier.update(superseded=True)
    return record_evidence(key=BUILT_YEAR_FACT, value=float(built.year), source_kind=FactSourceKind.EXTERNAL_SOURCE, source_name=built.source, wiki=wiki) is not None


def retract_wiki_build_year(wiki: Wiki, source: str) -> bool:
    """Withdraw a source's observations of a wiki's year once that source no longer dates it.

    Args:
        wiki: The wiki the year was about.
        source: The record it came from, a ``SOURCE_*`` name.

    Returns:
        Whether any observation was withdrawn.
    """
    from django.db import transaction

    from urbanlens.dashboard import tasks
    from urbanlens.dashboard.models.facts.model import Fact, FactEvidence, FactSourceKind
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    earlier = FactEvidence.objects.active().filter(fact__wiki=wiki, fact__key=BUILT_YEAR_FACT, source_kind=FactSourceKind.EXTERNAL_SOURCE, source_name=source)
    fact_ids = set(earlier.values_list("fact_id", flat=True))
    if not fact_ids:
        return False
    earlier.update(superseded=True)
    # As record_evidence does for an observation added.
    Fact.objects.filter(pk__in=fact_ids).update(needs_recompute=True, updated=timezone.now())
    for fact_id in fact_ids:
        transaction.on_commit(lambda fact_id=fact_id: safely_enqueue_task(tasks.recompute_fact_confidence, fact_id))
    return True


def wiki_build_year(wiki: Wiki) -> int | None:
    """The year a wiki's place was built, for its About card.

    The community's estimate once it has one; until then the newest record's. A record is one observation, which the
    fact's crowd threshold would otherwise never let show.

    Args:
        wiki: The wiki.

    Returns:
        The year, or None when nothing says.
    """
    from urbanlens.dashboard.models.facts.model import Fact, FactEvidence, FactSourceKind
    from urbanlens.dashboard.services.wiki.concealment import is_concealed

    if wiki.pk is None or is_concealed(wiki):
        return None
    fact = Fact.objects.filter(wiki=wiki, key=BUILT_YEAR_FACT).first()
    if fact is None:
        return None
    if fact.value_number is not None:
        return round(fact.value_number)
    latest = FactEvidence.objects.active().filter(fact=fact, source_kind=FactSourceKind.EXTERNAL_SOURCE, value_number__isnull=False).order_by("-created", "-pk").first()
    return round(latest.value_number) if latest is not None and latest.value_number is not None else None
