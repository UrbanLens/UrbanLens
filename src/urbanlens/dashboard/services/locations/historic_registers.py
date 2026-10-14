"""
Historic-register listings containing a location, as place-name candidates of the highest tier.

A source of register listings contributes a :class:`HistoricRegisterSource` to ``points.HISTORIC_REGISTERS``.
Only a listing whose own boundary contains the point is a candidate: registers list neighbours too.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.points import HISTORIC_REGISTERS
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.locations.name_tiers import NameTier

if TYPE_CHECKING:
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location


class HistoricRegisterSource(ABC):
    """A source of the register listings whose boundary contains a location."""

    #: The ``LocationCache.source`` values whose rows can carry a listing containing the point. A row saved under one
    #: refreshes the location's names.
    cache_sources: ClassVar[frozenset[str]]

    @abstractmethod
    def listing_names(self, location: Location) -> list[str]:
        """
        Names of the listings whose boundary contains the location, site-level first.

        On a building's own page only the building's own listings count: a campus listing's boundary holds most of
        the campus's buildings without listing them.

        Args:
            location: The location being named.
        """

    def listing_year(self, location: Location) -> int | None:
        """
        The year a listing containing the location dates its property to, or None.

        Args:
            location: The location being dated.
        """
        return None

    def names_new_building(self, row: LocationCache) -> bool:
        """
        Whether a cache row names a building whose own location is not already named after it.

        Args:
            row: A just-saved row of one of :attr:`cache_sources`.
        """
        return False


def register_listing_names(location: Location) -> list[str]:
    """
    Names of the register listings whose boundary contains this location, from every source.

    Args:
        location: The location being named.
    """
    return [name for source in HISTORIC_REGISTERS for name in source.listing_names(location)]


def register_listing_year(location: Location) -> int | None:
    """
    The year the first register listing that dates the location's property gives.

    Args:
        location: The location being dated.
    """
    return next((year for source in HISTORIC_REGISTERS if (year := source.listing_year(location)) is not None), None)


def register_cache_sources() -> frozenset[str]:
    """The cache sources whose rows can carry a register listing."""
    return frozenset(cache_source for source in HISTORIC_REGISTERS for cache_source in source.cache_sources)


def names_a_new_building(row: LocationCache) -> bool:
    """Whether a just-saved register-source row names a building whose own location is not already named after it."""
    return any(row.source in source.cache_sources and source.names_new_building(row) for source in HISTORIC_REGISTERS)


class HistoricRegisterNameProvider(NameProvider):
    """Contributes the names of the register listings containing a location."""

    def __init__(self) -> None:
        """Initialize the provider under the ``historic_register`` source slug."""
        super().__init__(source="historic_register", verbose_name="Historic registers", tier=NameTier.HISTORIC_REGISTER)

    def candidates(self, location: Location) -> list[str | None]:
        """The containing listings' names - see :func:`register_listing_names`."""
        return list(register_listing_names(location))
