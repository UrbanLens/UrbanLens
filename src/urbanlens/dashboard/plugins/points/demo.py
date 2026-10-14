"""Demo instances."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.demo.locations import DemoLocationSource


DEMO_LOCATION_SOURCES: ExtensionPoint[DemoLocationSource] = ExtensionPoint(
    "demo.location_sources",
    "Catalogs of real public places a demo instance's location pool is filled from when it has none, asked in priority order until one answers.",
)

__all__ = ["DEMO_LOCATION_SOURCES"]
