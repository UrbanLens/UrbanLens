"""Boundaries, building records and the place names and dates derived from them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider
    from urbanlens.dashboard.services.apis.locations.boundaries.overture import OvertureMirror
    from urbanlens.dashboard.services.floorplans.resolution import FloorplanProvider
    from urbanlens.dashboard.services.locations.historic_registers import HistoricRegisterSource
    from urbanlens.dashboard.services.places.building_records import BuildingRecordProvider


BOUNDARY_PROVIDERS: ExtensionPoint[type[BoundaryProvider]] = ExtensionPoint(
    "geo.boundary_providers",
    "Classes of the default-boundary providers, asked in priority order until both a property and a building outline are found. A provider whose property outlines may be voted on sets ``vote_source`` and ``vote_label``; the chain order is the vote's tie-break order. Core's public providers sit at 100-130.",
)

OVERTURE_MIRRORS: ExtensionPoint[OvertureMirror] = ExtensionPoint(
    "geo.overture_mirrors",
    "Regional copies of Overture Maps data. For a coordinate a mirror covers, Overture's public release is not read and the mirror answers instead, even when it holds nothing.",
)

BUILDING_RECORD_PROVIDERS: ExtensionPoint[BuildingRecordProvider] = ExtensionPoint(
    "geo.building_record_providers",
    "Sources of the building records on a parcel (see ``services.places.building_records`` for the record schema), asked in priority order; the first to find buildings answers. Core's public provider sits at 100.",
)

HISTORIC_REGISTERS: ExtensionPoint[HistoricRegisterSource] = ExtensionPoint(
    "geo.historic_registers",
    "Sources of the historic-register listings that contain a location: they name it, date it and refresh its names when their cached data arrives.",
)

FLOORPLAN_PROVIDERS: ExtensionPoint[FloorplanProvider] = ExtensionPoint(
    "geo.floorplan_providers",
    "Upstream sources of floorplans for a building, asked after the viewer's own and the community's. Their documents are read-only; saving creates the viewer's own version.",
)

__all__ = ["BOUNDARY_PROVIDERS", "BUILDING_RECORD_PROVIDERS", "FLOORPLAN_PROVIDERS", "HISTORIC_REGISTERS", "OVERTURE_MIRRORS"]
