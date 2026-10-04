"""Which of a campus's buildings a location stands on, and that building's wiki (P261).

A building with no place of its own resolves onto its parcel, whose wiki another location holds. The buildings known
on the parcel are the building wikis nested under the parcel's wiki and the building list cached for that wiki's
location; a location stands on one whose footprint holds it, else on the nearest within ``BUILDING_MATCH_METERS``.
Read from places, wikis and location data only - never from anyone's pins.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point

from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.building_clusters import BuildingCluster

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StandingBuilding:
    """The building of a campus's that a location stands on.

    Attributes:
        wiki: The building's wiki, or None while it has none.
        parent: The wiki a new one for the building nests under: the enclosing building's, else the campus's.
    """

    wiki: Wiki | None
    parent: Wiki


@dataclass(frozen=True, slots=True)
class _Building:
    """One known building: a cluster of records, a building wiki, or both."""

    cluster: BuildingCluster | None
    wiki: Wiki | None
    footprint: GEOSGeometry | None

    def holding_area(self, latitude: float, longitude: float) -> float | None:
        """The area of the smallest of this building's footprints holding the coordinate, or None."""
        areas = []
        if self.cluster is not None and (held := self.cluster.holding_footprint(latitude, longitude)) is not None:
            areas.append(held.area)
        if self.footprint is not None:
            point = Point(longitude, latitude, srid=4326)
            if self.footprint.contains(point) or self.footprint.touches(point):
                areas.append(self.footprint.area)
        return min(areas) if areas else None

    def meters(self, latitude: float, longitude: float) -> float:
        """Metres from the coordinate to the nearest of this building's points."""
        distances = []
        if self.cluster is not None:
            distances.append(self.cluster.nearest_meters(latitude, longitude))
        if self.wiki is not None:
            distances.append(meters_between(self.wiki.effective_latitude, self.wiki.effective_longitude, latitude, longitude))
        return min(distances)


def _wiki_footprint(wiki: Wiki) -> GEOSGeometry | None:
    """The outline of the building place a building's wiki holds or stands on, if it has one."""
    from urbanlens.dashboard.models.place.model import PlaceKind

    for place in (wiki.place if wiki.place_id else None, wiki.location.place if wiki.location.place_id else None):
        if place is not None and place.kind == PlaceKind.BUILDING and place.geometry is not None and not place.geometry.empty:
            return place.geometry
    return None


class CampusBuildings:
    """The buildings known on one campus: its wiki's building descendants and its location's cached building list."""

    def __init__(self, campus_wiki: Wiki, buildings: list[_Building], parents: dict[int, Wiki]) -> None:
        """Hold one campus's known buildings.

        Args:
            campus_wiki: The wiki holding the campus's place.
            buildings: Every known building.
            parents: Cluster id to the wiki of the nearest enclosing cluster that has one, else the campus's.
        """
        self.campus_wiki = campus_wiki
        self.buildings = buildings
        self._parents = parents

    @classmethod
    def of(cls, campus_wiki: Wiki) -> CampusBuildings | None:
        """The buildings known on the place ``campus_wiki`` holds.

        Args:
            campus_wiki: A wiki holding a place, with ``place`` and ``location`` loaded.

        Returns:
            None unless the place is a site or a parcel holding several buildings: on an ordinary property the
            parcel's wiki is its building's.
        """
        from urbanlens.dashboard.models.pin.model import PinType
        from urbanlens.dashboard.models.place.model import PlaceKind
        from urbanlens.dashboard.services.locations.site_scope import parcel_buildings
        from urbanlens.dashboard.services.pins.building_clusters import cluster_buildings, match_clusters
        from urbanlens.dashboard.services.pins.pin_restructure import MAX_RESTRUCTURE_ITEMS, building_markers, property_buildings
        from urbanlens.dashboard.services.places.scope import pin_type_for_place

        place = campus_wiki.place if campus_wiki.place_id else None
        if place is None or place.kind == PlaceKind.BUILDING or pin_type_for_place(place) != PinType.PARCEL:
            return None
        records = property_buildings(parcel_buildings(campus_wiki.location) or [], place.geometry)[:MAX_RESTRUCTURE_ITEMS]
        clusters = cluster_buildings(records, place.geometry)
        wikis = building_markers(campus_wiki.descendants().select_related("location__place", "place"))
        matched, unmatched = match_clusters(clusters, wikis)

        buildings = [_Building(cluster=cluster, wiki=matched.get(index), footprint=_wiki_footprint(matched[index]) if index in matched else None) for index, cluster in enumerate(clusters)]
        buildings += [_Building(cluster=None, wiki=wiki, footprint=_wiki_footprint(wiki)) for wiki in unmatched if wiki.pin_type == PinType.BUILDING]

        wiki_of = {id(cluster): matched[index] for index, cluster in enumerate(clusters) if index in matched}
        parents: dict[int, Wiki] = {}
        for cluster in clusters:
            enclosing = cluster.parent
            while enclosing is not None and id(enclosing) not in wiki_of:
                enclosing = enclosing.parent
            parents[id(cluster)] = wiki_of[id(enclosing)] if enclosing is not None else campus_wiki
        return cls(campus_wiki, buildings, parents)

    def standing(self, latitude: float, longitude: float) -> StandingBuilding | None:
        """The building a coordinate stands on: the smallest footprint holding it, else the nearest within range.

        Args:
            latitude: The coordinate's latitude.
            longitude: The coordinate's longitude.

        Returns:
            The building, or None for a coordinate on the grounds.
        """
        held = [(area, building.meters(latitude, longitude), index) for index, building in enumerate(self.buildings) if (area := building.holding_area(latitude, longitude)) is not None]
        if held:
            chosen = self.buildings[min(held)[2]]
        else:
            near = [(meters, index) for index, building in enumerate(self.buildings) if (meters := building.meters(latitude, longitude)) <= BUILDING_MATCH_METERS]
            if not near:
                return None
            chosen = self.buildings[min(near)[1]]
        if chosen.wiki is not None:
            return StandingBuilding(wiki=chosen.wiki, parent=self.campus_wiki)
        parent = self._parents.get(id(chosen.cluster), self.campus_wiki) if chosen.cluster is not None else self.campus_wiki
        return StandingBuilding(wiki=None, parent=parent)


def standing_building(location: Location, campus_wiki: Wiki) -> StandingBuilding | None:
    """Which of the buildings known on ``campus_wiki``'s place ``location`` stands on.

    Args:
        location: A location on the place, other than the campus wiki's own.
        campus_wiki: The wiki holding the place the location resolved onto.

    Returns:
        The building, or None when the location stands on none of them or the place is not a campus.
    """
    if location.pk == campus_wiki.location_id or location.latitude is None or location.longitude is None:
        return None
    buildings = CampusBuildings.of(campus_wiki)
    return buildings.standing(float(location.latitude), float(location.longitude)) if buildings is not None else None


def locations_awaiting_building_wikis(campus_wiki: Wiki) -> list[int]:
    """The pinned locations on ``campus_wiki``'s place standing on one of its buildings that has no wiki yet.

    A location counts only once a community member has pinned it, as on a pin save
    (``pin.signals.ensure_wiki_for_pin_location``).

    Args:
        campus_wiki: A wiki holding a place, with ``place`` and ``location`` loaded.

    Returns:
        Their PKs.
    """
    from urbanlens.dashboard.models.location.model import Location

    candidates = list(
        Location.objects.filter(place_id=campus_wiki.place_id, wiki__isnull=True, latitude__isnull=False, longitude__isnull=False, pins__profile__community_enabled=True)
        .exclude(pk=campus_wiki.location_id)
        .distinct()
        .only("pk", "latitude", "longitude"),
    )
    if not candidates:
        return []
    buildings = CampusBuildings.of(campus_wiki)
    if buildings is None:
        return []
    return [location.pk for location in candidates if (building := buildings.standing(float(location.latitude), float(location.longitude))) is not None and building.wiki is None]
