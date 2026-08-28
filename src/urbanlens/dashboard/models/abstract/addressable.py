"""Abstract mixin that adds structured address fields and derived properties to a model."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.abstract.model import DashboardModel

if TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import ForeignKey

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

_DEDUP_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

_IDENTITY_DISPLAY_ORDER = {"Place Name": 0, "Official Name": 1, "Address": 2}


def collapse_identity_fields(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the most detailed version of near-duplicate Place Name/Official Name/Address text.

    These three fields often carry the same core address at different levels of
    formatting completeness (e.g. "123 Main St" vs. "123 Main St, Springfield,
    IL 62704, USA"). A plain equality check only catches an exact match, not
    one string being a formatting-level superset of another.

    Args:
        candidates: ``(label, value)`` pairs. Labels should be Place Name,
            Official Name, and/or Address.

    Returns:
        The surviving pairs, restored to Place Name / Official Name / Address
        display order.
    """
    kept: list[tuple[str, str]] = []
    kept_normalized: list[str] = []
    for label, value in sorted(candidates, key=lambda pair: len(pair[1]), reverse=True):
        normalized = _DEDUP_NORMALIZE_RE.sub(" ", value.casefold()).strip()
        if any(normalized in existing for existing in kept_normalized):
            continue
        kept.append((label, value))
        kept_normalized.append(normalized)
    kept.sort(key=lambda pair: _IDENTITY_DISPLAY_ORDER.get(pair[0], 99))
    return kept


class AddressableModel(DashboardModel):
    """
    Abstract mixin that adds structured address fields and derived properties to a model.

    Children must define a ForeignKey to Location. TODO: Enforce this via Metaclass.
    """

    if TYPE_CHECKING:
        location: ForeignKey[Location]

    @property
    def latitude(self) -> Decimal:
        """Latitude of the linked Location."""
        return self.location.latitude

    @property
    def longitude(self) -> Decimal:
        """Longitude of the linked Location."""
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
        """State/province component of the linked Location's address, if known."""
        return self.location.state

    @property
    def county(self) -> str | None:
        """County component of the linked Location's address, if known."""
        return self.location.county

    @property
    def city(self) -> str | None:
        """City/locality component of the linked Location's address, if known."""
        return self.location.city

    @property
    def country(self) -> str | None:
        """Country component of the linked Location's address, if known."""
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
        """Best-known display name for the linked Location, if any."""
        return self.location.place_name

    @property
    def point(self):
        """PostGIS point of the linked Location, if any."""
        return self.location.point

    def has_place_name(self) -> bool:
        """True when the cached or resolved Google place name is useful for queries."""
        return self.location.has_place_name()

    @property
    def deduplicated_identity_fields(self) -> list[tuple[str, str]]:
        """(label, value) pairs for Place Name/Official Name/Address, with near-duplicates collapsed.

        Pin overrides this to prefer its ``effective_*`` name/address variants.
        Wiki uses the Location-backed values here directly.

        Returns:
            (label, value) pairs to render on a details/about card.
        """
        candidates: list[tuple[str, str]] = []
        if self.has_place_name() and self.place_name:
            candidates.append(("Place Name", self.place_name))
        if self.official_name:
            candidates.append(("Official Name", self.official_name))
        if self.address:
            candidates.append(("Address", self.address))
        return collapse_identity_fields(candidates)

    class Meta(DashboardModel.Meta):
        abstract = True
