"""Abstract mixin that adds structured address fields and derived properties to a model."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.abstract.model import DashboardModel

if TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import ForeignKey

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


class AddressableModel(DashboardModel):
    """
    Abstract mixin that adds structured address fields and derived properties to a model.
    
    Children must define a ForeignKey to Location. TODO: Enforce this via Metaclass.
    """
    if TYPE_CHECKING:
        location: ForeignKey[Location]
    
    @property
    def latitude(self) -> Decimal:
        return self.location.latitude
    
    @property
    def longitude(self) -> Decimal:
        return self.location.longitude

    @property
    def address(self) -> str | None:
        """Full address string built from components."""
        return self.location.address

    @property
    def address_basic(self) -> str | None:
        """Street number and route only."""
        return self.location.address_basic

    @property
    def address_extended(self) -> str | None:
        """Street address with city."""
        return self.location.address_extended

    @property
    def state(self) -> str | None:
        return self.location.state

    @property
    def county(self) -> str | None:
        return self.location.county

    @property
    def city(self) -> str | None:
        return self.location.city
    
    @property
    def country(self) -> str | None:
        return self.location.country

    @property
    def cached_place_name(self) -> str | None:
        """Google place name from the linked cache row, if any."""
        return self.location.cached_place_name

    @property
    def cid(self) -> Decimal | None:
        """Google Maps CID from the linked cache row, if any."""
        return self.location.cid

    @property
    def official_name(self) -> str | None:
        """External-source name from the linked Location."""
        return self.location.official_name
    
    @property
    def place_name(self) -> str | None:
        return self.location.place_name

    @property
    def point(self):
        """PostGIS point of the linked Location, if any."""
        return self.location.point
    
    def has_place_name(self) -> bool:
        """True when the cached or resolved Google place name is useful for queries."""
        return self.location.has_place_name()
    
    class Meta(DashboardModel.Meta):
        abstract = True
